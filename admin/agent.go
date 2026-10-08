package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

type Agent struct {
	mu             sync.Mutex
	runtime, token string
	client         *http.Client
}

func (g *Agent) probe() (string, bool) {
	res, e := g.client.Get("http://127.0.0.1:9090/revision")
	if e != nil {
		return "", false
	}
	body, e := io.ReadAll(io.LimitReader(res.Body, 256))
	res.Body.Close()
	if e != nil || res.StatusCode != 200 {
		return "", false
	}
	rev := strings.TrimSpace(string(body))
	res, e = g.client.Get("http://127.0.0.1:8080/readyz")
	if e != nil {
		return rev, false
	}
	res.Body.Close()
	return rev, res.StatusCode == 200
}
func (g *Agent) reload() error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if exec.CommandContext(ctx, "openresty", "-p", "/app/", "-c", "nginx/nginx.conf", "-t").Run() != nil {
		return errors.New("nginx_validation_failed")
	}
	return exec.CommandContext(ctx, "openresty", "-p", "/app/", "-c", "nginx/nginx.conf", "-s", "reload").Run()
}
func (g *Agent) waitRevision(id string) bool {
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		rev, healthy := g.probe()
		if rev == id && healthy {
			return true
		}
		time.Sleep(200 * time.Millisecond)
	}
	return false
}
func (g *Agent) activate(id string) error {
	if !identifier.MatchString(id) {
		return errors.New("invalid_revision")
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	path := filepath.Join(g.runtime, "config.json")
	previous, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	data, e := os.ReadFile(filepath.Join(g.runtime, "revisions", id+".json"))
	if e != nil {
		return e
	}
	var value Obj
	if json.Unmarshal(data, &value) != nil || value["revision"] != id {
		return errors.New("invalid_snapshot")
	}
	var old Obj
	if json.Unmarshal(previous, &old) != nil {
		return errors.New("invalid_previous_snapshot")
	}
	if e = atomicFile(path, data); e == nil {
		e = g.reload()
	}
	if e == nil && !g.waitRevision(id) {
		e = errors.New("revision_unhealthy")
	}
	if e != nil {
		if atomicFile(path, previous) != nil || g.reload() != nil || !g.waitRevision(stringValue(old["revision"])) {
			return errors.New("rollback_failed")
		}
		return errors.New("activation_failed")
	}
	return nil
}
func (g *Agent) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !equal(r.Header.Get("Authorization"), "Bearer "+g.token) {
		fail(w, 401, "invalid_control_key")
		return
	}
	if r.Method != "POST" {
		fail(w, 405, "method_not_allowed")
		return
	}
	var b struct {
		Revision string `json:"revision"`
	}
	if !decode(w, r, &b) {
		return
	}
	switch r.URL.Path {
	case "/status":
		rev, healthy := g.probe()
		reply(w, 200, Obj{"revision": rev, "healthy": healthy})
	case "/activate":
		if e := g.activate(b.Revision); e != nil {
			fail(w, 503, "activation_failed")
			return
		}
		reply(w, 200, Obj{"revision": b.Revision, "healthy": true})
	default:
		fail(w, 404, "not_found")
	}
}
func runAgent() error {
	token, e := secretFile("ADMIN_INTERNAL_TOKEN")
	if e != nil {
		return e
	}
	g := &Agent{runtime: env("ADMIN_RUNTIME", "/runtime"), token: token, client: &http.Client{Timeout: 2 * time.Second, Transport: &http.Transport{DisableKeepAlives: true}}}
	path := filepath.Join(g.runtime, "config.json")
	raw, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	var snapshot Obj
	if json.Unmarshal(raw, &snapshot) != nil {
		return errors.New("invalid_runtime")
	}
	if snapshot["revision"] == nil {
		snapshot["revision"] = "bootstrap"
		raw = encode(snapshot)
		if e = atomicFile(path, raw); e != nil {
			return e
		}
	}
	if e = atomicFile(filepath.Join(g.runtime, "revisions", stringValue(snapshot["revision"])+".json"), raw); e != nil {
		return e
	}
	sink := &eventWriter{path: filepath.Join(env("ADMIN_LOG_DIR", "/logs"), "gateway.jsonl")}
	cmd := exec.Command("openresty", "-p", "/app/", "-c", "nginx/nginx.conf", "-g", "daemon off;")
	cmd.Stdout = io.MultiWriter(os.Stdout, sink)
	cmd.Stderr = io.MultiWriter(os.Stderr, sink)
	if e = cmd.Start(); e != nil {
		return e
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	server := &http.Server{Addr: ":9081", Handler: g, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 40 * time.Second}
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.ListenAndServe() }()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.Signal(15))
	defer signal.Stop(signals)
	select {
	case e = <-done:
		_ = server.Close()
		return e
	case e = <-serveErr:
		_ = cmd.Process.Kill()
		return e
	case <-signals:
	}
	_ = server.Close()
	_ = cmd.Process.Signal(syscall.Signal(3))
	select {
	case e = <-done:
		return e
	case <-time.After(40 * time.Second):
		_ = cmd.Process.Kill()
		return <-done
	}
}

type eventWriter struct {
	mu      sync.Mutex
	pending []byte
	path    string
}

func (s *eventWriter) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pending = append(s.pending, p...)
	for {
		idx := bytes.IndexByte(s.pending, '\n')
		if idx < 0 {
			break
		}
		line := append([]byte(nil), s.pending[:idx]...)
		s.pending = s.pending[idx+1:]
		s.line(line)
	}
	if len(s.pending) > 65536 {
		s.pending = nil
	}
	return len(p), nil
}
func (s *eventWriter) line(line []byte) {
	start := bytes.IndexByte(line, '{')
	end := bytes.LastIndexByte(line, '}')
	if start < 0 || end <= start {
		return
	}
	var event Obj
	if json.Unmarshal(line[start:end+1], &event) != nil || event["event"] == nil {
		return
	}
	allowed := strings.Fields("event request_id attempt_id trace_id user_id agent_id model actual_model provider started_at finished_at prompt_tokens completion_tokens total_tokens usage_source usage_status status http_status reason from_attempt_id to_attempt_id from_provider to_provider from_model to_model from_key_ref to_key_ref error_code config_revision")
	out := Obj{"time": time.Now().Unix()}
	for _, k := range allowed {
		if v, ok := event[k]; ok {
			switch v.(type) {
			case string, float64, bool, nil:
				out[k] = v
			}
		}
	}
	_ = os.MkdirAll(filepath.Dir(s.path), 0700)
	if info, e := os.Stat(s.path); e == nil && info.Size() > 8<<20 {
		_ = os.Remove(s.path + ".8")
		for i := 7; i >= 1; i-- {
			_ = os.Rename(fmt.Sprintf("%s.%d", s.path, i), fmt.Sprintf("%s.%d", s.path, i+1))
		}
		_ = os.Rename(s.path, s.path+".1")
	}
	f, e := os.OpenFile(s.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if e == nil {
		_, _ = f.Write(append(encode(out), '\n'))
		_ = f.Close()
	}
}
