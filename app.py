# ---------- Supabase upsert (salvar cookies) ----------
def _supabase_upsert_cookie(domain: str, cookies_txt: str) -> dict:
    """
    Tenta salvar no seu esquema atual (host + cookie_text).
    Se falhar, tenta no esquema antigo (domain + cookies_txt).
    Retorna um dict com {"ok": bool, "schema": "new"|"old"|None, "status": int, "text": str}
    """
    if not _supabase_can_use():
        return {"ok": False, "schema": None, "status": 400, "text": "SUPABASE not configured"}

    url = f"{SUPABASE_URL}/rest/v1/cookies"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        # merge-duplicates faz upsert quando há unique constraint/PK compatível
        "Prefer": "resolution=merge-duplicates",
    }

    # 1) esquema novo (host + cookie_text)
    payload_new = {"host": domain, "cookie_text": cookies_txt}
    r = requests.post(url, headers=headers, json=payload_new, timeout=15)
    if r.status_code in (200, 201, 204):
        return {"ok": True, "schema": "new", "status": r.status_code, "text": ""}

    # 2) fallback esquema antigo (domain + cookies_txt)
    payload_old = {"domain": domain, "cookies_txt": cookies_txt}
    r2 = requests.post(url, headers=headers, json=payload_old, timeout=15)
    if r2.status_code in (200, 201, 204):
        return {"ok": True, "schema": "old", "status": r2.status_code, "text": ""}

    return {
        "ok": False,
        "schema": None,
        "status": r2.status_code,
        "text": r2.text if hasattr(r2, "text") else str(r2),
    }
    
