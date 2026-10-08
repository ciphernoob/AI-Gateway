package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestGatewayKeyReveal(t *testing.T) {
	a := fixture(t)
	key := "gw_reveal-test-secret"
	d := Document{Config: Obj{"api_keys": []any{Obj{"key_env": "GW", "disabled": true}}, "models": Obj{}}, Secrets: map[string]string{"GW": "id", "PROVIDER": "provider"}}
	if _, err := a.db.Exec("INSERT INTO draft VALUES(1,1,?)", string(encode(d))); err != nil {
		t.Fatal(err)
	}
	if _, err := a.db.Exec("INSERT INTO credentials VALUES(?,?),(?,?)", "id", a.encrypt("id", key), "provider", a.encrypt("provider", "upstream-private")); err != nil {
		t.Fatal(err)
	}
	login := req(a, "POST", "/login", Obj{"username": "admin", "password": "test-admin-password"}, "", "")
	var session Obj
	json.Unmarshal(login.Body.Bytes(), &session)
	cookie := login.Result().Cookies()[0].String()
	csrf := stringValue(session["csrf"])
	path := "/keys/GW/reveal"
	if req(a, "POST", path, Obj{}, "", "").Code != 401 {
		t.Fatal("unauthenticated reveal")
	}
	if req(a, "POST", path, Obj{}, cookie, "").Code != 403 {
		t.Fatal("missing CSRF")
	}
	for _, ref := range []string{"PROVIDER", "missing", "GW/extra"} {
		if req(a, "POST", "/keys/"+ref+"/reveal", Obj{}, cookie, csrf).Code != 404 {
			t.Fatal("non gateway reference accepted")
		}
	}
	w := req(a, "POST", path, Obj{}, cookie, csrf)
	if w.Code != 200 || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("reveal response", w.Code)
	}
	var result Obj
	json.Unmarshal(w.Body.Bytes(), &result)
	if result["key"] != key {
		t.Fatal("incorrect decrypted value")
	}
	var event, target, resultText string
	if err := a.db.QueryRow("SELECT event,target,result FROM operations WHERE event='gateway_key.reveal'").Scan(&event, &target, &resultText); err != nil {
		t.Fatal(err)
	}
	if target != "GW" || strings.Contains(event+target+resultText, key) {
		t.Fatal("invalid audit")
	}
	draft := req(a, "GET", "/draft", nil, cookie, "")
	if strings.Contains(draft.Body.String(), key) {
		t.Fatal("draft leaked key")
	}
	_, _ = a.db.Exec("UPDATE credentials SET value=? WHERE id='id'", []byte("broken"))
	if req(a, "POST", path, Obj{}, cookie, csrf).Code != 503 {
		t.Fatal("corrupt secret accepted")
	}
	_, _ = a.db.Exec("UPDATE credentials SET value=? WHERE id='id'", a.encrypt("id", key))
	_, _ = a.db.Exec("DROP TABLE operations")
	w = req(a, "POST", path, Obj{}, cookie, csrf)
	if w.Code != 503 || strings.Contains(w.Body.String(), key) {
		t.Fatal("audit failure leaked key")
	}
	_, _ = a.db.Exec("UPDATE sessions SET expires=0")
	if req(a, "POST", path, Obj{}, cookie, csrf).Code != 401 {
		t.Fatal("expired session accepted")
	}
}
