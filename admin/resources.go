package main

import (
	"database/sql"
	"errors"
	"net/http"
	"regexp"
	"strings"
	"time"
)

var identifier = regexp.MustCompile(`^[A-Za-z0-9_-]{1,80}$`)

func only(v Obj, fields string) bool {
	allow := map[string]bool{}
	for _, f := range strings.Fields(fields) {
		allow[f] = true
	}
	for k := range v {
		if !allow[k] {
			return false
		}
	}
	return true
}
func (a *App) mutate(w http.ResponseWriter, r *http.Request, path string) {
	var b struct {
		Revision int64  `json:"revision"`
		Value    Obj    `json:"value"`
		Secret   string `json:"secret"`
	}
	if !decode(w, r, &b) {
		return
	}
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) < 1 || len(parts) > 2 {
		fail(w, 404, "not_found")
		return
	}
	resource := parts[0]
	id := ""
	if len(parts) == 2 {
		id = parts[1]
		if !identifier.MatchString(id) {
			fail(w, 400, "invalid_identifier")
			return
		}
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	doc, rev, e := a.draft()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	if b.Revision != rev {
		apiError(w, errConflict)
		return
	}
	tx, e := a.db.Begin()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	defer tx.Rollback()
	newKey := ""
	target := resource + ":" + id
	switch resource {
	case "providers":
		if id == "" || !only(b.Value, "base_url disabled quota_exhaustion_codes") {
			fail(w, 400, "invalid_provider")
			return
		}
		values := object(doc.Config["providers"])
		if r.Method == "DELETE" {
			delete(values, id)
		} else {
			old := object(values[id])
			ref := stringValue(old["key_env"])
			if ref == "" {
				ref = "PROVIDER_" + strings.ToUpper(randomID())
			}
			if b.Secret != "" {
				if len(b.Secret) < 16 || len(b.Secret) > 4096 || strings.ContainsAny(b.Secret, "\r\n\t") {
					fail(w, 400, "invalid_secret")
					return
				}
				sid, err := a.putSecret(tx, b.Secret)
				if err != nil {
					fail(w, 503, "database_unavailable")
					return
				}
				doc.Secrets[ref] = sid
			}
			if doc.Secrets[ref] == "" {
				fail(w, 400, "secret_required")
				return
			}
			b.Value["key_env"] = ref
			values[id] = b.Value
		}
		doc.Config["providers"] = values
	case "users":
		if id == "" || r.Method == "DELETE" || !only(b.Value, "daily_token_limit monthly_token_limit disabled") {
			fail(w, 400, "invalid_user")
			return
		}
		object(doc.Config["users"])[id] = b.Value
	case "models":
		if id == "" || !only(b.Value, "candidates budget_limit") {
			fail(w, 400, "invalid_model")
			return
		}
		models := object(doc.Config["models"])
		limits := object(object(doc.Config["budget"])["model_limits"])
		if r.Method == "DELETE" {
			delete(models, id)
			delete(limits, id)
		} else {
			list, ok := b.Value["candidates"].([]any)
			if !ok || len(list) < 1 || len(list) > 2 {
				fail(w, 400, "invalid_candidates")
				return
			}
			for _, c := range list {
				if !only(object(c), "provider model context_tokens max_output_tokens n_max input_rate output_rate price_version capabilities supports_stream_usage") {
					fail(w, 400, "invalid_candidate")
					return
				}
			}
			if _, ok := b.Value["budget_limit"]; !ok {
				fail(w, 400, "budget_required")
				return
			}
			limits[id] = b.Value["budget_limit"]
			models[id] = Obj{"candidates": list}
		}
	case "keys":
		keys, _ := doc.Config["api_keys"].([]any)
		if id == "" && r.Method == "POST" {
			if !only(b.Value, "user_id agent_id scopes agent_budget") {
				fail(w, 400, "invalid_key")
				return
			}
			if object(doc.Config["users"])[stringValue(b.Value["user_id"])] == nil {
				fail(w, 400, "invalid_user")
				return
			}
			newKey = "gw_" + randomID()
			ref := "GATEWAY_" + strings.ToUpper(randomID())
			sid, err := a.putSecret(tx, newKey)
			if err != nil {
				fail(w, 503, "database_unavailable")
				return
			}
			doc.Secrets[ref] = sid
			b.Value["key_env"] = ref
			b.Value["disabled"] = false
			if agent := stringValue(b.Value["agent_id"]); agent != "" {
				if !identifier.MatchString(agent) {
					fail(w, 400, "invalid_agent")
					return
				}
				limits := object(object(doc.Config["budget"])["agent_limits"])
				if _, exists := limits[agent]; !exists {
					if b.Value["agent_budget"] == nil {
						fail(w, 400, "agent_budget_required")
						return
					}
					limits[agent] = b.Value["agent_budget"]
				}
			}
			delete(b.Value, "agent_budget")
			keys = append(keys, b.Value)
		} else if r.Method == "DELETE" && id != "" {
			found := false
			for _, v := range keys {
				k := object(v)
				if k["key_env"] == id {
					k["disabled"] = true
					found = true
				}
			}
			if !found {
				fail(w, 404, "not_found")
				return
			}
		} else {
			fail(w, 405, "method_not_allowed")
			return
		}
		doc.Config["api_keys"] = keys
	case "settings":
		if id != "" || !only(b.Value, "public_url fallback global_limit agent_limits") {
			fail(w, 400, "invalid_settings")
			return
		}
		u := stringValue(b.Value["public_url"])
		if !validPublicURL(u) {
			fail(w, 400, "invalid_public_url")
			return
		}
		doc.PublicURL = strings.TrimRight(u, "/")
		if v, ok := b.Value["fallback"]; ok {
			doc.Config["fallback"] = v
		}
		budget := object(doc.Config["budget"])
		if v, ok := b.Value["global_limit"]; ok {
			budget["global_limit"] = v
		}
		if v, ok := b.Value["agent_limits"]; ok {
			budget["agent_limits"] = v
		}
	default:
		fail(w, 404, "not_found")
		return
	}
	result, e := tx.Exec("UPDATE draft SET revision=revision+1,document=? WHERE id=1 AND revision=?", string(encode(doc)), rev)
	if e == nil {
		n, _ := result.RowsAffected()
		if n != 1 {
			e = errConflict
		}
	}
	if e == nil {
		_, e = tx.Exec("INSERT INTO operations(time,event,target,result) VALUES(?,?,?,?)", time.Now().Unix(), "draft."+r.Method, target, "ok")
	}
	if e == nil {
		e = tx.Commit()
	}
	if e != nil {
		apiError(w, e)
		return
	}
	reply(w, 200, Obj{"revision": rev + 1, "key": newKey, "pending_publication": true})
}
func (a *App) preview(w http.ResponseWriter, r *http.Request) {
	next, _, e := a.draft()
	if id := r.URL.Query().Get("version"); id != "" {
		next, e = a.version(id)
	}
	if e != nil {
		fail(w, 404, "version_not_found")
		return
	}
	previous, id, e := a.active()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	reply(w, 200, Obj{"active_version": id, "before": previous, "after": next, "note": "回滚包含用户及 Key 状态，可能恢复此前禁用的凭证。secret_refs 的变化只表示密钥版本变化，不包含密钥值。"})
}
func pagination(r *http.Request) (int, int, error) {
	q := r.URL.Query()
	limit := 50
	offset := 0
	for k, p := range map[string]*int{"limit": &limit, "offset": &offset} {
		if s := q.Get(k); s != "" {
			var n int
			for _, c := range s {
				if c < '0' || c > '9' {
					return 0, 0, errors.New("invalid_page")
				}
				n = n*10 + int(c-'0')
				if n > 1000000 {
					return 0, 0, errors.New("invalid_page")
				}
			}
			*p = n
		}
	}
	if limit < 1 || limit > 100 {
		return 0, 0, errors.New("invalid_page")
	}
	return limit, offset, nil
}
func (a *App) versionsHTTP(w http.ResponseWriter, r *http.Request) {
	limit, offset, e := pagination(r)
	if e != nil {
		fail(w, 400, "invalid_page")
		return
	}
	rows, e := a.db.Query("SELECT id,status,created,error FROM versions ORDER BY created DESC,id DESC LIMIT ? OFFSET ?", limit+1, offset)
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	defer rows.Close()
	items := []Obj{}
	for rows.Next() {
		var id, status, err string
		var created int64
		if rows.Scan(&id, &status, &created, &err) != nil {
			fail(w, 503, "database_unavailable")
			return
		}
		items = append(items, Obj{"id": id, "status": status, "created": created, "error": err})
	}
	more := len(items) > limit
	if more {
		items = items[:limit]
	}
	reply(w, 200, Obj{"data": items, "has_more": more})
}
func (a *App) operationsHTTP(w http.ResponseWriter, r *http.Request) {
	limit, offset, e := pagination(r)
	if e != nil {
		fail(w, 400, "invalid_page")
		return
	}
	rows, e := a.db.Query("SELECT id,time,event,target,result FROM operations ORDER BY id DESC LIMIT ? OFFSET ?", limit+1, offset)
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	defer rows.Close()
	items := []Obj{}
	for rows.Next() {
		var id, t int64
		var event, target, result string
		if rows.Scan(&id, &t, &event, &target, &result) != nil {
			fail(w, 503, "database_unavailable")
			return
		}
		items = append(items, Obj{"id": id, "time": t, "event": event, "target": target, "result": result})
	}
	more := len(items) > limit
	if more {
		items = items[:limit]
	}
	reply(w, 200, Obj{"data": items, "has_more": more})
}

var _ = sql.ErrNoRows
