package main

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"database/sql"
	"encoding/json"
	"golang.org/x/crypto/bcrypt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T) *App {
	t.Helper()
	db, e := sql.Open("sqlite", filepath.Join(t.TempDir(), "admin.db"))
	if e != nil {
		t.Fatal(e)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { db.Close() })
	_, e = db.Exec(`CREATE TABLE settings(name TEXT PRIMARY KEY,value TEXT);CREATE TABLE sessions(id TEXT PRIMARY KEY,csrf TEXT,expires INTEGER);CREATE TABLE credentials(id TEXT PRIMARY KEY,value BLOB);CREATE TABLE draft(id INTEGER PRIMARY KEY,revision INTEGER,document TEXT);CREATE TABLE versions(id TEXT PRIMARY KEY,document TEXT,status TEXT,created INTEGER,error TEXT DEFAULT '');CREATE TABLE operations(id INTEGER PRIMARY KEY,time INTEGER,event TEXT,target TEXT,result TEXT);`)
	if e != nil {
		t.Fatal(e)
	}
	block, _ := aes.NewCipher(make([]byte, 32))
	gcm, _ := cipher.NewGCM(block)
	hash, _ := bcrypt.GenerateFromPassword([]byte("test-admin-password"), bcrypt.MinCost)
	_, _ = db.Exec("INSERT INTO settings VALUES('password',?),('username','admin')", string(hash))
	return &App{db: db, aead: gcm, client: &http.Client{Timeout: time.Second}, failures: map[string]*loginBucket{}, static: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "UI") })}
}
func req(a *App, method, path string, body any, cookie, csrf string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "https://localhost/admin/api/v1"+path, bytes.NewReader(encode(body)))
	r.RemoteAddr = "127.0.0.1:1234"
	if cookie != "" {
		r.Header.Set("Cookie", cookie)
	}
	if csrf != "" {
		r.Header.Set("X-CSRF-Token", csrf)
	}
	w := httptest.NewRecorder()
	a.ServeHTTP(w, r)
	return w
}
func TestSessionAndCSRF(t *testing.T) {
	a := fixture(t)
	if got := req(a, "GET", "/draft", nil, "", "").Code; got != 401 {
		t.Fatal(got)
	}
	w := req(a, "POST", "/login", Obj{"username": "admin", "password": "test-admin-password"}, "", "")
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	var data Obj
	_ = json.Unmarshal(w.Body.Bytes(), &data)
	cookie := w.Result().Cookies()[0]
	if !cookie.Secure || !cookie.HttpOnly || cookie.SameSite != http.SameSiteStrictMode {
		t.Fatal("insecure cookie")
	}
	if req(a, "POST", "/logout", Obj{}, cookie.String(), "").Code != 403 {
		t.Fatal("missing CSRF accepted")
	}
	if req(a, "GET", "/session", nil, cookie.String(), "").Code != 200 {
		t.Fatal("session invalid")
	}
	_, _ = a.db.Exec("UPDATE sessions SET expires=0")
	if req(a, "GET", "/session", nil, cookie.String(), "").Code != 401 {
		t.Fatal("expired session accepted")
	}
	_, _ = a.db.Exec("UPDATE sessions SET expires=?", time.Now().Add(time.Hour).Unix())
	if req(a, "POST", "/logout", Obj{}, cookie.String(), stringValue(data["csrf"])).Code != 200 {
		t.Fatal("logout failed")
	}
	if req(a, "GET", "/session", nil, cookie.String(), "").Code != 401 {
		t.Fatal("revoked session accepted")
	}
}
func TestLoginRateLimit(t *testing.T) {
	a := fixture(t)
	for i := 0; i < 5; i++ {
		if req(a, "POST", "/login", Obj{"username": "admin", "password": "wrong"}, "", "").Code != 401 {
			t.Fatal(i)
		}
	}
	if req(a, "POST", "/login", Obj{"username": "admin", "password": "test-admin-password"}, "", "").Code != 429 {
		t.Fatal("not rate limited")
	}
}
func TestCredentialEncryptionBoundToID(t *testing.T) {
	a := fixture(t)
	encrypted := a.encrypt("id1", "provider-secret-value")
	if bytes.Contains(encrypted, []byte("provider-secret-value")) {
		t.Fatal("plaintext")
	}
	value, e := a.decrypt("id1", encrypted)
	if e != nil || value != "provider-secret-value" {
		t.Fatal(e)
	}
	if _, e = a.decrypt("id2", encrypted); e == nil {
		t.Fatal("credential substitution accepted")
	}
	encrypted[len(encrypted)-1] ^= 1
	if _, e = a.decrypt("id1", encrypted); e == nil {
		t.Fatal("corruption accepted")
	}
}
func TestDraftConflictAndKeyNonDisclosure(t *testing.T) {
	a := fixture(t)
	d := Document{Config: Obj{"users": Obj{"u": Obj{}}, "api_keys": []any{}, "budget": Obj{"agent_limits": Obj{}}}, Secrets: map[string]string{}}
	_, _ = a.db.Exec("INSERT INTO draft VALUES(1,1,?)", string(encode(d)))
	_, _ = a.db.Exec("INSERT INTO sessions VALUES(?,?,?)", digest("session"), "csrf", time.Now().Add(time.Hour).Unix())
	cookie := "admin_session=session"
	w := req(a, "POST", "/keys", Obj{"revision": 1, "value": Obj{"user_id": "u", "scopes": []string{"chat:write"}}}, cookie, "csrf")
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	var result Obj
	_ = json.Unmarshal(w.Body.Bytes(), &result)
	key := stringValue(result["key"])
	if len(key) < 32 {
		t.Fatal("weak key")
	}
	w = req(a, "GET", "/draft", nil, cookie, "")
	if strings.Contains(w.Body.String(), key) {
		t.Fatal("key leaked")
	}
	w = req(a, "PUT", "/users/u", Obj{"revision": 1, "value": Obj{"disabled": true}}, cookie, "csrf")
	if w.Code != 409 {
		t.Fatal("conflict accepted")
	}
	d, rev, _ := a.draft()
	if rev != 2 || object(object(d.Config["users"])["u"])["disabled"] == true {
		t.Fatal("conflict modified state")
	}
}
func TestUserUpdateRejectsUnexpectedFields(t *testing.T) {
	a := fixture(t)
	_, _ = a.db.Exec("INSERT INTO sessions VALUES(?,?,?)", digest("s"), "c", time.Now().Add(time.Hour).Unix())
	d := Document{Config: Obj{"users": Obj{"u": Obj{}}}, Secrets: map[string]string{}}
	_, _ = a.db.Exec("INSERT INTO draft VALUES(1,1,?)", string(encode(d)))
	if req(a, "PUT", "/users/u", Obj{"revision": 1, "value": Obj{"password": "must-not-store"}}, "admin_session=s", "c").Code != 400 {
		t.Fatal("unexpected field accepted")
	}
}
func TestEventLogFiltersContentAndRotates(t *testing.T) {
	path := filepath.Join(t.TempDir(), "gateway.jsonl")
	s := &eventWriter{path: path}
	s.Write([]byte("noise\nnotice {\"event\":\"fallback.selected\",\"request_id\":\"r\",\"body\":\"secret body\",\"api_key\":\"secret key\"}\n"))
	b, e := os.ReadFile(path)
	if e != nil || bytes.Contains(b, []byte("secret")) {
		t.Fatal("content leak", e)
	}
	f, _ := os.OpenFile(path, os.O_WRONLY, 0600)
	_ = f.Truncate((8 << 20) + 1)
	f.Close()
	s.Write([]byte("{\"event\":\"request\"}\n"))
	if _, e := os.Stat(path + ".1"); e != nil {
		t.Fatal("rotation", e)
	}
}
func TestUsageUnknownAndRetention(t *testing.T) {
	v := usageWindow(Obj{"total_tokens": int64(15), "unknown_attempts": int64(1)}, "2026-09", float64(100), false)
	if v["remaining_is_exact"] != false || v["remaining_tokens"] != int64(85) {
		t.Fatal(v)
	}
	v = usageWindow(Obj{}, "2020-01", nil, true)
	if v["available"] != false || v["total_tokens"] != nil {
		t.Fatal("expired reported zero")
	}
}
func TestTokenOnlyDocumentRemovesMoneyFields(t *testing.T) {
	d := Document{Config: Obj{"plugins": Obj{"budget": true, "user_quota": true}, "budget": Obj{"global_limit": 1},
		"models": Obj{"coding": Obj{"candidates": []any{Obj{"provider": "p", "model": "m", "input_rate": 1, "output_rate": 2, "price_version": "v1"}}}}}}
	d = tokenOnlyDocument(d)
	if d.Config["budget"] != nil || object(d.Config["plugins"])["budget"] != nil {
		t.Fatal("budget survived migration")
	}
	candidate := object(object(object(d.Config["models"])["coding"])["candidates"].([]any)[0])
	for _, field := range []string{"input_rate", "output_rate", "price_version"} {
		if candidate[field] != nil {
			t.Fatal("money field survived", field)
		}
	}
}
func TestControlAuthenticationAndTraversal(t *testing.T) {
	g := &Agent{token: "private", runtime: t.TempDir()}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/status", strings.NewReader("{}")))
	if w.Code != 401 {
		t.Fatal(w.Code)
	}
	if g.activate("../config") == nil {
		t.Fatal("path traversal")
	}
}
