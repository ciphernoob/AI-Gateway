package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"path/filepath"
	"time"
)

type dependencyError struct{ Status int }

func (e dependencyError) Error() string { return "dependency_rejected" }

func (a *App) internal(base, path string, body any, out any) error {
	req, e := http.NewRequest("POST", base+path, bytes.NewReader(encode(body)))
	if e != nil {
		return e
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	req.Header.Set("Content-Type", "application/json")
	res, e := a.client.Do(req)
	if e != nil {
		return errors.New("dependency_unavailable")
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return dependencyError{Status: res.StatusCode}
	}
	if out != nil {
		return json.NewDecoder(http.MaxBytesReader(nil, res.Body, 16<<20)).Decode(out)
	}
	return nil
}
func (a *App) publishHTTP(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Revision int64  `json:"revision"`
		Version  string `json:"version"`
	}
	if !decode(w, r, &b) {
		return
	}
	if !a.publish.TryLock() {
		fail(w, 409, "publication_in_progress")
		return
	}
	defer a.publish.Unlock()
	var pending int
	if a.db.QueryRow("SELECT COUNT(*) FROM versions WHERE status IN ('publishing','recovery_required')").Scan(&pending) != nil || pending > 0 {
		fail(w, 409, "publication_recovery_required")
		return
	}
	d, rev, e := a.draft()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	if b.Revision != rev {
		apiError(w, errConflict)
		return
	}
	if b.Version != "" {
		d, e = a.version(b.Version)
		if e != nil {
			fail(w, 404, "version_not_found")
			return
		}
	}
	id := "v_" + randomID()
	snapshot, e := a.compile(d, id)
	if e != nil {
		fail(w, 422, "invalid_configuration")
		return
	}
	_, previous, e := a.active()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	_, e = a.db.Exec("INSERT INTO versions(id,document,status,created) VALUES(?,?,'publishing',?)", id, string(encode(d)), time.Now().Unix())
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	if e = atomicFile(filepath.Join(a.runtime, "revisions", id+".json"), encode(snapshot)); e == nil {
		e = a.internal(a.auditURL, "/admin/redaction", Obj{"revision": id}, nil)
	}
	if e == nil {
		e = a.internal(a.agent, "/activate", Obj{"revision": id}, nil)
	}
	if e != nil {
		status := "failed"
		var state Obj
		if a.internal(a.agent, "/status", Obj{}, &state) != nil || state["revision"] != previous {
			if a.internal(a.agent, "/activate", Obj{"revision": previous}, nil) != nil {
				status = "recovery_required"
			}
		}
		_, _ = a.db.Exec("UPDATE versions SET status=?,error='publication_failed' WHERE id=?", status, id)
		_ = a.operation("config.publish", id, status)
		fail(w, 503, status)
		return
	}
	if e = a.markActive(id); e != nil {
		fail(w, 503, "publication_recovery_required")
		return
	}
	_ = a.operation("config.publish", id, "ok")
	reply(w, 200, Obj{"version": id, "status": "active"})
}
func (a *App) markActive(id string) error {
	tx, e := a.db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	if _, e = tx.Exec("UPDATE versions SET status='superseded' WHERE status='active'"); e != nil {
		return e
	}
	if _, e = tx.Exec("UPDATE versions SET status='active',error='' WHERE id=?", id); e != nil {
		return e
	}
	return tx.Commit()
}
func (a *App) recoverPublication() {
	a.publish.Lock()
	defer a.publish.Unlock()
	rows, e := a.db.Query("SELECT id FROM versions WHERE status IN ('publishing','recovery_required') ORDER BY created")
	if e != nil {
		return
	}
	var ids []string
	for rows.Next() {
		var id string
		_ = rows.Scan(&id)
		ids = append(ids, id)
	}
	rows.Close()
	for _, id := range ids {
		var state Obj
		for i := 0; i < 20; i++ {
			e = a.internal(a.agent, "/status", Obj{}, &state)
			if e == nil {
				break
			}
			time.Sleep(time.Second)
		}
		if e != nil {
			_, _ = a.db.Exec("UPDATE versions SET status='recovery_required',error='control_unavailable' WHERE id=?", id)
			continue
		}
		if state["revision"] == id && state["healthy"] == true {
			_ = a.markActive(id)
			_ = a.operation("config.recover", id, "active")
			continue
		}
		_, previous, err := a.active()
		if err == nil {
			err = a.internal(a.agent, "/activate", Obj{"revision": previous}, nil)
		}
		status := "failed"
		if err != nil {
			status = "recovery_required"
		}
		_, _ = a.db.Exec("UPDATE versions SET status=?,error='interrupted_publication' WHERE id=?", status, id)
		_ = a.operation("config.recover", id, status)
	}
}
