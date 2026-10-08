package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/redis/go-redis/v9"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

var zone = time.FixedZone("Asia/Shanghai", 8*3600)

func (a *App) redisClient(d Document) (*redis.Client, error) {
	secrets, e := a.secrets(d)
	if e != nil {
		return nil, e
	}
	cfg := object(d.Config["redis"])
	return redis.NewClient(&redis.Options{Addr: fmt.Sprintf("%s:%d", stringValue(cfg["host"]), number(cfg["port"])), Password: secrets[stringValue(cfg["password_env"])], DialTimeout: time.Second, ReadTimeout: 2 * time.Second, WriteTimeout: 2 * time.Second}), nil
}
func hashResult(value any) Obj {
	result := Obj{}
	list, _ := value.([]any)
	for i := 0; i+1 < len(list); i += 2 {
		k, _ := list[i].(string)
		v, _ := strconv.ParseInt(fmt.Sprint(list[i+1]), 10, 64)
		result[k] = v
	}
	return result
}
func usageWindow(fields Obj, period string, limit any, expired bool) Obj {
	if expired {
		return Obj{"period": period, "available": false, "reason": "retention_expired"}
	}
	out := Obj{"period": period, "available": true, "token_limit": limit, "remaining_tokens": nil, "remaining_is_exact": false}
	for _, key := range []string{"prompt_tokens", "completion_tokens", "total_tokens", "pending_attempts", "unknown_attempts"} {
		out[key] = number(fields[key])
	}
	if limit != nil {
		remaining := number(limit) - number(out["total_tokens"])
		if remaining < 0 {
			remaining = 0
		}
		out["remaining_tokens"] = remaining
		out["remaining_is_exact"] = number(out["pending_attempts"]) == 0 && number(out["unknown_attempts"]) == 0
	}
	return out
}
func addUsage(target, value Obj) {
	for _, key := range []string{"prompt_tokens", "completion_tokens", "total_tokens", "pending_attempts", "unknown_attempts"} {
		target[key] = number(target[key]) + number(value[key])
	}
}
func (a *App) usageHTTP(w http.ResponseWriter, r *http.Request) {
	d, id, e := a.active()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	limit, offset, e := pagination(r)
	if e != nil {
		fail(w, 400, "invalid_page")
		return
	}
	now := time.Now().In(zone)
	day := r.URL.Query().Get("day")
	if day == "" {
		day = now.Format("2006-01-02")
	}
	month := r.URL.Query().Get("month")
	if month == "" {
		month = day[:min(7, len(day))]
	}
	dt, e1 := time.ParseInLocation("2006-01-02", day, zone)
	mt, e2 := time.ParseInLocation("2006-01", month, zone)
	if e1 != nil || e2 != nil || dt.After(now) || mt.After(now) {
		fail(w, 400, "invalid_period")
		return
	}
	retention := object(d.Config["usage"])
	dayExpired := now.After(dt.AddDate(0, 0, 1+int(number(retention["daily_retention_days"]))))
	monthExpired := now.After(mt.AddDate(0, 1, int(number(retention["monthly_retention_days"]))))
	users := object(d.Config["users"])
	models := object(d.Config["models"])
	modelFilter := r.URL.Query().Get("model")
	if modelFilter != "" && models[modelFilter] == nil {
		fail(w, 404, "model_not_found")
		return
	}
	modelIDs := []string{}
	for model := range models {
		if modelFilter == "" || model == modelFilter {
			modelIDs = append(modelIDs, model)
		}
	}
	sort.Strings(modelIDs)
	ids := []string{}
	filter := r.URL.Query().Get("user_id")
	for uid := range users {
		if filter == "" || uid == filter {
			ids = append(ids, uid)
		}
	}
	sort.Strings(ids)
	if filter != "" && len(ids) == 0 {
		fail(w, 404, "user_not_found")
		return
	}
	total := len(ids)
	if offset > total {
		offset = total
	}
	end := min(offset+limit, total)
	ids = ids[offset:end]
	client, e := a.redisClient(d)
	if e != nil {
		fail(w, 503, "redis_unavailable")
		return
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	items := []Obj{}
	for _, uid := range ids {
		keys := []string{}
		for _, model := range modelIDs {
			keys = append(keys, "quota:usage:"+uid+":"+model+":"+day, "quota:usage:"+uid+":"+model+":"+month)
		}
		v, err := client.Eval(ctx, `local out={};for i=1,#KEYS do out[i]=redis.call('HGETALL',KEYS[i]) end;return out`, keys).Slice()
		if err != nil {
			fail(w, 503, "redis_unavailable")
			return
		}
		user := object(users[uid])
		var dailyLimit, monthlyLimit any
		if modelFilter == "" {
			dailyLimit = user["daily_token_limit"]
			monthlyLimit = user["monthly_token_limit"]
		}
		dailyTotal, monthlyTotal := Obj{}, Obj{}
		modelUsage := []Obj{}
		for i, model := range modelIDs {
			dailyFields, monthlyFields := hashResult(v[i*2]), hashResult(v[i*2+1])
			addUsage(dailyTotal, dailyFields)
			addUsage(monthlyTotal, monthlyFields)
			modelUsage = append(modelUsage, Obj{"model": model,
				"daily":   usageWindow(dailyFields, day, nil, dayExpired),
				"monthly": usageWindow(monthlyFields, month, nil, monthExpired)})
		}
		items = append(items, Obj{"user_id": uid, "disabled": user["disabled"] == true, "models": modelUsage,
			"daily":   usageWindow(dailyTotal, day, dailyLimit, dayExpired),
			"monthly": usageWindow(monthlyTotal, month, monthlyLimit, monthExpired)})
	}
	reply(w, 200, Obj{"data": items, "total": total, "has_more": end < total, "timezone": "Asia/Shanghai", "enforcement": false, "consistency": "eventual", "config_revision": id})
}
func (a *App) overview(w http.ResponseWriter, r *http.Request) {
	d, id, e := a.active()
	if e != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	var state Obj
	if e = a.internal(a.agent, "/status", Obj{}, &state); e != nil {
		state = Obj{"healthy": false, "error": "gateway_unavailable"}
	}
	c, e := a.redisClient(d)
	if e != nil {
		fail(w, 503, "redis_unavailable")
		return
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	pending, e := c.ZCard(ctx, "usage:pending").Result()
	if e != nil {
		fail(w, 503, "redis_unavailable")
		return
	}
	reply(w, 200, Obj{"gateway": state, "redis": Obj{"healthy": true}, "config_revision": id,
		"pending_attempts": pending, "accounting": "provider_tokens_only",
		"user_count": len(object(d.Config["users"])), "model_count": len(object(d.Config["models"]))})
}
func (a *App) auditHTTP(w http.ResponseWriter, r *http.Request, path string) {
	parts := strings.Split(path, "/")
	resource := ""
	id := ""
	if path == "requests" {
		resource = "requests"
	} else if len(parts) == 2 && (parts[0] == "requests" || parts[0] == "traces") && identifier.MatchString(parts[1]) {
		id = parts[1]
		resource = "request"
		if parts[0] == "traces" {
			resource = "trace"
		}
	} else {
		fail(w, 404, "not_found")
		return
	}
	params := Obj{}
	for k, v := range r.URL.Query() {
		if len(v) != 1 {
			fail(w, 400, "invalid_query")
			return
		}
		params[k] = v[0]
	}
	if a.operation("audit.read", resource+":"+id, "requested") != nil {
		fail(w, 503, "database_unavailable")
		return
	}
	var result Obj
	if e := a.internal(a.auditURL, "/admin/query", Obj{"resource": resource, "id": id, "params": params}, &result); e != nil {
		var rejected dependencyError
		if errors.As(e, &rejected) && (rejected.Status == 400 || rejected.Status == 404) {
			fail(w, rejected.Status, map[int]string{400: "invalid_query", 404: "not_found"}[rejected.Status])
			return
		}
		fail(w, 503, "audit_unavailable")
		return
	}
	reply(w, 200, result)
}
func (a *App) logsHTTP(w http.ResponseWriter, r *http.Request) {
	limit, offset, e := pagination(r)
	if e != nil {
		fail(w, 400, "invalid_page")
		return
	}
	q := r.URL.Query()
	start, end := int64(0), time.Now().Unix()
	for k, p := range map[string]*int64{"from": &start, "to": &end} {
		if v := q.Get(k); v != "" {
			*p, e = strconv.ParseInt(v, 10, 64)
			if e != nil {
				fail(w, 400, "invalid_period")
				return
			}
		}
	}
	if start > end {
		fail(w, 400, "invalid_period")
		return
	}
	items := []Obj{}
	index := 0
	more := false
	found := false
	// Newest file first; each file is bounded by rotation, pages have hard limits.
	for file := 0; file <= 8; file++ {
		name := filepath.Join(env("ADMIN_LOG_DIR", "/logs"), "gateway.jsonl")
		if file > 0 {
			name += fmt.Sprintf(".%d", file)
		}
		f, err := os.Open(name)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			fail(w, 503, "logs_unavailable")
			return
		}
		found = true
		entries := []Obj{}
		scan := bufio.NewScanner(f)
		scan.Buffer(make([]byte, 4096), 65536)
		for scan.Scan() {
			var v Obj
			if json.Unmarshal(scan.Bytes(), &v) != nil {
				continue
			}
			t := number(v["time"])
			if t < start || t > end {
				continue
			}
			match := true
			for _, key := range []string{"event", "user_id", "model", "request_id"} {
				if value := q.Get(key); value != "" && v[key] != value {
					match = false
				}
			}
			if match {
				entries = append(entries, v)
			}
		}
		err = scan.Err()
		f.Close()
		if err != nil {
			fail(w, 503, "logs_unavailable")
			return
		}
		for i := len(entries) - 1; i >= 0; i-- {
			if index >= offset {
				if len(items) == limit {
					more = true
					break
				}
				items = append(items, entries[i])
			}
			index++
		}
		if more {
			break
		}
	}
	reply(w, 200, Obj{"data": items, "has_more": more, "available": found, "retention": "9 files, 8 MiB each"})
}
