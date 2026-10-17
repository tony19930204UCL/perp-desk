"""Adapter for the actual exported commitment ledger contract."""
def ledger_items(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("ledger must be an object with items")
    return list(data["items"])

def cron_evidence(job_id, outputs, journal=(), files=()):
    """Normalize caller-read evidence; performs no filesystem access."""
    runs=[]
    for row in outputs:
        if row.get("job_id") == job_id:
            runs.append({"job_id":job_id, "at":row.get("at"), "ref":row.get("ref"),
                         "content":row.get("content"), "status":row.get("status","ok"),
                         "script_hash":row.get("script_hash")})
    return {"runs":runs, "journal":list(journal), "files":list(files)}
