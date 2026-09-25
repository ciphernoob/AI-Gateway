package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"golang.org/x/crypto/bcrypt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

func reply(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func fail(w http.ResponseWriter, status int, code string) {
	reply(w, status, Obj{"error": Obj{"code": code, "message": code}})
}
func decode(w http.ResponseWriter, r *http.Request, out any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	d := json.NewDecoder(r.Body)
	if e := d.Decode(out); e != nil {
		fail(w, 400, "invalid_json")
		return false
	}
	var more any
	if d.Decode(&more) != io.EOF {
		fail(w, 400, "invalid_json")
		return false
	}
	return true
}
func digest(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
func equal(a, b string) bool { return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1 }
func (a *App) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Strict-Transport-Security", "max-age=31536000")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
	if !strings.HasPrefix(r.URL.Path, "/admin/api/") {
		if r.Method != "GET" && r.Method != "HEAD" {
			fail(w, 405, "method_not_allowed")
			return
		}
		if !strings.HasPrefix(r.URL.Path, "/assets/") {
			r.URL.Path = "/"
		}
		a.static.ServeHTTP(w, r)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	path := strings.TrimPrefix(r.URL.Path, "/admin/api/v1")
	if !strings.HasPrefix(r.URL.Path, "/admin/api/v1/") {
		fail(w, 404, "not_found")
		return
	}
	if path == "/login" && r.Method == "POST" {
		a.login(w, r)
		return
	}
	cookie, e := r.Cookie("admin_session")
	if e != nil {
		fail(w, 401, "login_required")
		return
	}
	var csrf string
	var expires int64
	e = a.db.QueryRow("SELECT csrf,expires FROM sessions WHERE id=?", digest(cookie.Value)).Scan(&csrf, &expires)
	if e != nil || time.Now().Unix() >= expires {
		fail(w, 401, "session_expired")
		return
	}
	if r.Method != "GET" {
		if !equal(r.Header.Get("X-CSRF-Token"), csrf) {
			fail(w, 403, "csrf_invalid")
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && origin != "https://"+r.Host {
			fail(w, 403, "origin_invalid")
			return
		}
	}
	switch {
	case path == "/session" && r.Method == "GET":
		reply(w, 200, Obj{"username": env("ADMIN_USERNAME", "admin"), "csrf": csrf})
	case path == "/logout" && r.Method == "POST":
		_, _ = a.db.Exec("DELETE FROM sessions WHERE id=?", digest(cookie.Value))
		http.SetCookie(w, &http.Cookie{Name: "admin_session", Value: "", Path: "/", Secure: true, HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: -1})
		reply(w, 200, Obj{"ok": true})
	case path == "/draft" && r.Method == "GET":
		d, rev, e := a.draft()
		if e != nil {
			fail(w, 503, "database_unavailable")
			return
		}
		reply(w, 200, Obj{"revision": rev, "document": d})
	case path == "/validate" && r.Method == "POST":
		d, _, e := a.draft()
		if e == nil {
			_, e = a.compile(d, "validation")
		}
		if e != nil {
			fail(w, 422, "invalid_configuration")
			return
		}
		reply(w, 200, Obj{"valid": true})
	case path == "/preview" && r.Method == "GET":
		a.preview(w, r)
	case path == "/publish" && r.Method == "POST":
		a.publishHTTP(w, r)
	case path == "/versions" && r.Method == "GET":
		a.versionsHTTP(w, r)
	case path == "/overview" && r.Method == "GET":
		a.overview(w, r)
	case path == "/usage" && r.Method == "GET":
		a.usageHTTP(w, r)
	case path == "/logs" && r.Method == "GET":
		a.logsHTTP(w, r)
	case path == "/operations" && r.Method == "GET":
		a.operationsHTTP(w, r)
	case strings.HasPrefix(path, "/audit/") && r.Method == "GET":
		a.auditHTTP(w, r, strings.TrimPrefix(path, "/audit/"))
	case r.Method == "PUT" || r.Method == "POST" || r.Method == "DELETE":
		a.mutate(w, r, path)
	default:
		fail(w, 404, "not_found")
	}
}
func (a *App) login(w http.ResponseWriter, r *http.Request) {
	if origin := r.Header.Get("Origin"); origin != "" && origin != "https://"+r.Host {
		fail(w, 403, "origin_invalid")
		return
	}
	var b struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if !decode(w, r, &b) {
		return
	}
	host, _, _ := net.SplitHostPort(r.RemoteAddr)
	a.mu.Lock()
	now := time.Now()
	for ip, entry := range a.failures {
		if now.After(entry.Until) {
			delete(a.failures, ip)
		}
	}
	bucket := a.failures[host]
	if bucket != nil && bucket.Count >= 5 {
		a.mu.Unlock()
		w.Header().Set("Retry-After", "900")
		fail(w, 429, "login_rate_limited")
		return
	}
	if bucket == nil {
		if len(a.failures) >= 10000 {
			a.mu.Unlock()
			fail(w, 429, "login_rate_limited")
			return
		}
		bucket = &loginBucket{Until: now.Add(15 * time.Minute)}
		a.failures[host] = bucket
	}
	bucket.Count++
	a.mu.Unlock()
	var hash, user string
	if a.db.QueryRow("SELECT value FROM settings WHERE name='password'").Scan(&hash) != nil || a.db.QueryRow("SELECT value FROM settings WHERE name='username'").Scan(&user) != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	valid := bcrypt.CompareHashAndPassword([]byte(hash), []byte(b.Password)) == nil
	if !valid || !equal(user, b.Username) {
		_ = a.operation("admin.login", "admin", "denied")
		fail(w, 401, "invalid_credentials")
		return
	}
	a.mu.Lock()
	delete(a.failures, host)
	a.mu.Unlock()
	token, csrf := randomID(), randomID()
	expires := time.Now().Add(8 * time.Hour)
	_, e := a.db.Exec("DELETE FROM sessions WHERE expires<=?", time.Now().Unix())
	if e == nil {
		_, e = a.db.Exec("INSERT INTO sessions VALUES(?,?,?)", digest(token), csrf, expires.Unix())
	}
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	if a.operation("admin.login", "admin", "ok") != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	http.SetCookie(w, &http.Cookie{Name: "admin_session", Value: token, Path: "/", Secure: true, HttpOnly: true, SameSite: http.SameSiteStrictMode, Expires: expires})
	reply(w, 200, Obj{"csrf": csrf, "username": user})
}
func validPublicURL(value string) bool {
	u, e := url.Parse(value)
	return e == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != "" && u.User == nil && u.RawQuery == "" && u.Fragment == ""
}
func apiError(w http.ResponseWriter, e error) {
	if errors.Is(e, errConflict) {
		fail(w, 409, "revision_conflict")
	} else {
		fail(w, 422, "invalid_configuration")
	}
}
