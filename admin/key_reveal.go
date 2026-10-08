package main

import "net/http"

// Called only after the shared administrator session and CSRF checks.
func (a *App) revealGatewayKey(w http.ResponseWriter, r *http.Request, ref string) {
	doc, _, err := a.draft()
	if err != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	keys, _ := doc.Config["api_keys"].([]any)
	found := false
	for _, value := range keys {
		if object(value)["key_env"] == ref {
			found = true
			break
		}
	}
	if !found || doc.Secrets[ref] == "" {
		fail(w, 404, "gateway_key_not_found")
		return
	}
	id := doc.Secrets[ref]
	var encrypted []byte
	if err = a.db.QueryRow("SELECT value FROM credentials WHERE id=?", id).Scan(&encrypted); err != nil {
		fail(w, 503, "credential_unavailable")
		return
	}
	key, err := a.decrypt(id, encrypted)
	if err != nil {
		fail(w, 503, "credential_unavailable")
		return
	}
	if a.operation("gateway_key.reveal", ref, "ok") != nil {
		fail(w, 503, "operation_audit_unavailable")
		return
	}
	reply(w, 200, Obj{"key": key, "key_ref": ref})
}
