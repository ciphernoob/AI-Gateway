package main

import (
	"crypto/tls"
	"embed"
	"io/fs"
	"log"
	"net/http"
	"os"
	"time"
)

//go:embed web/dist*
var webFiles embed.FS

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
func main() {
	if len(os.Args) > 1 && os.Args[1] == "agent" {
		if err := runAgent(); err != nil {
			log.Fatal(err)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "init-secrets" {
		if err := initSecrets(); err != nil {
			log.Fatal(err)
		}
		return
	}
	if _, err := fs.Stat(webFiles, "web/dist/index.html"); err != nil {
		log.Fatal("Vue assets missing; run scripts/build-admin before starting the management server")
	}
	cert, key := env("ADMIN_TLS_CERT", "/certs/tls.crt"), env("ADMIN_TLS_KEY", "/certs/tls.key")
	if _, err := tls.LoadX509KeyPair(cert, key); err != nil {
		log.Fatal("valid TLS certificate and key required")
	}
	app, err := newApp()
	if err != nil {
		log.Fatal(err)
	}
	defer app.db.Close()
	files, _ := fs.Sub(webFiles, "web/dist")
	app.static = http.FileServer(http.FS(files))
	go app.recoverPublication()
	server := &http.Server{Addr: env("ADMIN_ADDR", "0.0.0.0:8443"), Handler: app, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 20 * time.Second, WriteTimeout: 100 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32768, TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
	log.Print("admin HTTPS listening on ", server.Addr)
	log.Fatal(server.ListenAndServeTLS(cert, key))
}
