package main

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/crypto/bcrypt"
	_ "modernc.org/sqlite"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

type Obj = map[string]any
type Document struct {
	Config    Obj               `json:"config"`
	Secrets   map[string]string `json:"secret_refs"`
	PublicURL string            `json:"public_url"`
}
type App struct {
	db                              *sql.DB
	aead                            cipher.AEAD
	mu                              sync.Mutex
	publish                         sync.Mutex
	static                          http.Handler
	client                          *http.Client
	runtime, token, agent, auditURL string
	failures                        map[string]*loginBucket
}
type loginBucket struct {
	Count int
	Until time.Time
}

var errConflict = errors.New("revision_conflict")

func randomID() string {
	b := make([]byte, 24)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}
func encode(v any) []byte {
	b, e := json.Marshal(v)
	if e != nil {
		panic(e)
	}
	return b
}
func object(v any) Obj {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return Obj{}
}
func stringValue(v any) string { s, _ := v.(string); return s }
func number(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case int:
		return int64(n)
	case int64:
		return n
	}
	return 0
}
func clone(v Obj) Obj { var out Obj; _ = json.Unmarshal(encode(v), &out); return out }
func secretFile(name string) (string, error) {
	b, e := os.ReadFile(env(name+"_FILE", "/secrets/"+name))
	if e != nil {
		return "", fmt.Errorf("required secret %s unavailable", name)
	}
	return string(bytes.TrimSpace(b)), nil
}

func newApp() (*App, error) {
	master, e := secretFile("ADMIN_MASTER_KEY")
	if e != nil {
		return nil, e
	}
	key, e := base64.StdEncoding.DecodeString(master)
	if e != nil || len(key) != 32 {
		return nil, errors.New("invalid master key")
	}
	block, _ := aes.NewCipher(key)
	aead, _ := cipher.NewGCM(block)
	path := env("ADMIN_DATABASE", "/data/admin.db")
	if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return nil, e
	}
	db, e := sql.Open("sqlite", path)
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(1)
	a := &App{db: db, aead: aead, client: &http.Client{Timeout: 45 * time.Second}, runtime: env("ADMIN_RUNTIME", "/runtime"), agent: env("ADMIN_AGENT_URL", "http://gateway:9081"), auditURL: env("ADMIN_AUDIT_URL", "http://audit-store:8001"), failures: map[string]*loginBucket{}}
	if a.token, e = secretFile("ADMIN_INTERNAL_TOKEN"); e != nil {
		return nil, e
	}
	_, e = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
 CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS credentials(id TEXT PRIMARY KEY,value BLOB NOT NULL);
 CREATE TABLE IF NOT EXISTS draft(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,document TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,document TEXT NOT NULL,status TEXT NOT NULL,created INTEGER NOT NULL,error TEXT NOT NULL DEFAULT '');
 CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY AUTOINCREMENT,time INTEGER NOT NULL,event TEXT NOT NULL,target TEXT NOT NULL,result TEXT NOT NULL);`)
	if e != nil {
		return nil, e
	}
	_ = os.Chmod(path, 0600)
	var hash string
	e = db.QueryRow("SELECT value FROM settings WHERE name='password'").Scan(&hash)
	if e == sql.ErrNoRows {
		pwd, err := secretFile("ADMIN_PASSWORD")
		if err != nil {
			return nil, err
		}
		if len(pwd) < 8 || len(pwd) > 72 {
			return nil, errors.New("administrator password must be 8..72 bytes")
		}
		h, err := bcrypt.GenerateFromPassword([]byte(pwd), bcrypt.DefaultCost)
		if err != nil {
			return nil, err
		}
		if _, e = db.Exec("INSERT INTO settings VALUES('password',?),('username',?)", string(h), env("ADMIN_USERNAME", "admin")); e != nil {
			return nil, e
		}
	} else if e != nil {
		return nil, e
	}
	var count int
	if e = db.QueryRow("SELECT COUNT(*) FROM draft").Scan(&count); e != nil {
		return nil, e
	}
	if count == 0 {
		if e = a.importConfig(); e != nil {
			return nil, e
		}
	}
	// Detect a lost/wrong master key at startup, not at the next publication.
	doc, _, err := a.draft()
	if err != nil {
		return nil, err
	}
	if _, err = a.secrets(doc); err != nil {
		return nil, errors.New("stored credentials cannot be decrypted with ADMIN_MASTER_KEY")
	}
	return a, nil
}
func compiler(input any, out any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, env("ADMIN_PYTHON", "python"), "-m", "scripts.admin_config")
	cmd.Dir = env("ADMIN_PROJECT", "/app")
	cmd.Stdin = bytes.NewReader(encode(input))
	data, e := cmd.Output()
	if e != nil {
		return errors.New("invalid_configuration")
	}
	if e = json.Unmarshal(data, out); e != nil {
		return errors.New("compiler_response_invalid")
	}
	return nil
}
func (a *App) encrypt(id, value string) []byte {
	nonce := make([]byte, a.aead.NonceSize())
	_, _ = rand.Read(nonce)
	return a.aead.Seal(nonce, nonce, []byte(value), []byte(id))
}
func (a *App) decrypt(id string, b []byte) (string, error) {
	n := a.aead.NonceSize()
	if len(b) < n {
		return "", errors.New("credential_corrupt")
	}
	raw, e := a.aead.Open(nil, b[:n], b[n:], []byte(id))
	return string(raw), e
}
func (a *App) putSecret(tx *sql.Tx, value string) (string, error) {
	id := randomID()
	_, e := tx.Exec("INSERT INTO credentials VALUES(?,?)", id, a.encrypt(id, value))
	return id, e
}
func (a *App) importConfig() error {
	var imported struct {
		Config  Obj               `json:"config"`
		Secrets map[string]string `json:"secrets"`
	}
	if e := compiler(Obj{"op": "import", "path": env("ADMIN_IMPORT", "/config/gateway.yaml")}, &imported); e != nil {
		return e
	}
	tx, e := a.db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	doc := Document{Config: imported.Config, Secrets: map[string]string{}, PublicURL: env("GATEWAY_PUBLIC_URL", "http://localhost:8080")}
	for name, value := range imported.Secrets {
		id, err := a.putSecret(tx, value)
		if err != nil {
			return err
		}
		doc.Secrets[name] = id
	}
	if _, e = tx.Exec("INSERT INTO draft VALUES(1,1,?)", string(encode(doc))); e != nil {
		return e
	}
	if _, e = tx.Exec("INSERT INTO versions(id,document,status,created) VALUES('bootstrap',?,'active',?)", string(encode(doc)), time.Now().Unix()); e != nil {
		return e
	}
	return tx.Commit()
}
func (a *App) draft() (Document, int64, error) {
	var d Document
	var raw string
	var rev int64
	e := a.db.QueryRow("SELECT revision,document FROM draft WHERE id=1").Scan(&rev, &raw)
	if e == nil {
		e = json.Unmarshal([]byte(raw), &d)
	}
	return d, rev, e
}
func (a *App) version(id string) (Document, error) {
	var d Document
	var raw string
	e := a.db.QueryRow("SELECT document FROM versions WHERE id=?", id).Scan(&raw)
	if e == nil {
		e = json.Unmarshal([]byte(raw), &d)
	}
	return d, e
}
func (a *App) active() (Document, string, error) {
	var id string
	e := a.db.QueryRow("SELECT id FROM versions WHERE status='active' ORDER BY created DESC LIMIT 1").Scan(&id)
	if e != nil {
		return Document{}, "", e
	}
	d, e := a.version(id)
	return d, id, e
}
func (a *App) secrets(d Document) (map[string]string, error) {
	out := map[string]string{}
	for name, id := range d.Secrets {
		var b []byte
		if e := a.db.QueryRow("SELECT value FROM credentials WHERE id=?", id).Scan(&b); e != nil {
			return nil, e
		}
		v, e := a.decrypt(id, b)
		if e != nil {
			return nil, e
		}
		out[name] = v
	}
	return out, nil
}
func (a *App) compile(d Document, id string) (Obj, error) {
	secrets, e := a.secrets(d)
	if e != nil {
		return nil, e
	}
	var snapshot Obj
	e = compiler(Obj{"config": d.Config, "secrets": secrets, "revision": id}, &snapshot)
	return snapshot, e
}
func (a *App) operation(event, target, result string) error {
	_, e := a.db.Exec("INSERT INTO operations(time,event,target,result) VALUES(?,?,?,?)", time.Now().Unix(), event, target, result)
	return e
}
func atomicFile(path string, data []byte) error {
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".pending-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(data)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e == nil {
		e = os.Rename(name, path)
	}
	return e
}
