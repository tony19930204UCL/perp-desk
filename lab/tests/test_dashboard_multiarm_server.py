"""HTTP request-line and status contracts on a real ephemeral loopback socket."""
import http.client
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dashboard_multiarm_server as s


class HttpTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.app=s.create_server(data_root=self.root,port=0,clock_ms=lambda:1000)
        self.thread=threading.Thread(target=self.app.serve_forever,
                                     kwargs={'poll_interval':.01},daemon=True)
        self.thread.start();self.addCleanup(self.stop)
    def stop(self):
        self.app.shutdown();self.app.server_close();self.thread.join(timeout=3)
    def request(self,method='GET',route='/'):
        c=http.client.HTTPConnection('127.0.0.1',self.app.server_port,timeout=3)
        try:
            c.request(method,route)
            r=c.getresponse()
            return r.status,dict(r.getheaders()),r.read()
        finally:c.close()
    def raw(self,method,target):
        with socket.create_connection(('127.0.0.1',self.app.server_port),timeout=3) as c:
            c.sendall(('%s %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'%(method,target)).encode())
            content=b''
            while True:
                part=c.recv(65536)
                if not part:break
                content+=part
        head,_,body=content.partition(b'\r\n\r\n')
        return int(head.split()[1]),body
    def test_bind(self):
        self.assertEqual(self.app.server_address[0],'127.0.0.1')
    def test_html(self):
        code,h,b=self.request()
        self.assertEqual(code,200)
        self.assertEqual(h['Content-Type'],'text/html; charset=utf-8')
        self.assertEqual(b,(s.ASSETS/'dashboard_multiarm.html').read_bytes())
    def test_js(self):
        self.assertEqual(self.request(route='/dashboard_multiarm_ui.js')[0],200)
    def test_json(self):
        code,h,b=self.request(route='/api/multiarm')
        self.assertEqual(code,200)
        self.assertEqual(len(json.loads(b)['arms']),12)
        self.assertTrue(all(a['state']=='unknown' for a in json.loads(b)['arms']))
    def test_head(self):
        for route in ('/','/dashboard_multiarm.html','/dashboard_multiarm_ui.js','/api/multiarm','/no'):
            with self.subTest(route=route):
                a=self.request('GET',route);b=self.request('HEAD',route)
                self.assertEqual(a[:2],b[:2]);self.assertEqual(b[2],b'')
                self.assertEqual(int(b[1]['Content-Length']),len(a[2]))
    def test_methods(self):
        for method in ('POST','PUT','PATCH','DELETE','OPTIONS','TRACE','CONNECT','BREW'):
            with self.subTest(method=method):
                status,headers,body=self.request(method)
                self.assertEqual(status,405)
                self.assertEqual(headers['Allow'],'GET, HEAD')
    def test_headers(self):
        for route in ('/','/api/multiarm','/bad'):
            a=self.request(route=route)
            self.assertEqual(a[1]['Cache-Control'],'no-store')
            self.assertEqual(a[1]['X-Content-Type-Options'],'nosniff')
    def test_no_source_paths(self):
        body=self.request(route='/api/multiarm')[2]
        self.assertNotIn(str(self.root).encode(),body)


def bad_route_case(route):
    def test(self):
        code,body=self.raw('GET',route)
        self.assertEqual(code,404)
        self.assertEqual(json.loads(body),{'error':'找不到資源'})
    return test


for idx,path in enumerate(('//','//api/multiarm','/%2e','/../dashboard_multiarm.html',
                            '/dashboard_multiarm.html?x=1','/api/multiarm?arm=h2',
                            '/api/multiarm/h2','/dashboard_multiarm_ui.js%2e',
                            '/%2fapi/multiarm','/api/../api/multiarm',
                            'http://localhost/api/multiarm','/paper-v4',
                            '/dashboard_multiarm.html/','/missing')):
    setattr(HttpTests,'test_raw_%02d'%idx,bad_route_case(path))


if __name__=='__main__':unittest.main()
