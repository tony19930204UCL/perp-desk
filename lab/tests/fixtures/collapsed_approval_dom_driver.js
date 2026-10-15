// Isolated candidate-JS driver helper, no network or live profile.
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const page=path.join(__dirname,'collapsed_approval_dom.html');
if (!fs.existsSync(page)) throw Error('fixture missing');
const html=fs.readFileSync(page,'utf8');
if (!html.includes('syntheticClicks') || !html.includes('prompt-textarea')) throw Error('fixture invalid');
process.stdout.write('FIXTURE_SOURCE_OK (not DOM execution)\n');
