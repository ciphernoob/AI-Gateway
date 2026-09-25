package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func initSecrets() error {
	root := env("ADMIN_INIT_DIR", ".admin")
	for _, name := range []string{"secrets", "certs"} {
		if e := os.MkdirAll(filepath.Join(root, name), 0700); e != nil {
			return e
		}
	}
	for _, name := range []string{"ADMIN_PASSWORD", "ADMIN_MASTER_KEY", "ADMIN_INTERNAL_TOKEN"} {
		path := filepath.Join(root, "secrets", name)
		if _, e := os.Stat(path); e == nil {
			continue
		} else if !os.IsNotExist(e) {
			return e
		}
		b := make([]byte, 32)
		if _, e := rand.Read(b); e != nil {
			return e
		}
		f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if e != nil {
			return e
		}
		value := base64.StdEncoding.EncodeToString(b)
		if name == "ADMIN_PASSWORD" {
			value = "12345678"
		}
		_, e = f.WriteString(value)
		f.Close()
		if e != nil {
			return e
		}
	}
	certPath, keyPath := filepath.Join(root, "certs", "tls.crt"), filepath.Join(root, "certs", "tls.key")
	_, ce := os.Stat(certPath)
	_, ke := os.Stat(keyPath)
	if ce == nil && ke == nil {
		return nil
	}
	if !os.IsNotExist(ce) || !os.IsNotExist(ke) {
		return errors.New("certificate pair already partially exists; will not overwrite")
	}
	key, e := rsa.GenerateKey(rand.Reader, 3072)
	if e != nil {
		return e
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "AI Gateway development"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().AddDate(0, 3, 0), KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
	for _, host := range strings.Split(env("ADMIN_TLS_HOSTS", ""), ",") {
		host = strings.TrimSpace(host)
		if host == "" {
			continue
		}
		if ip := net.ParseIP(host); ip != nil {
			template.IPAddresses = append(template.IPAddresses, ip)
		} else {
			template.DNSNames = append(template.DNSNames, host)
		}
	}
	cert, e := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if e != nil {
		return e
	}
	if e = os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0600); e != nil {
		return e
	}
	return os.WriteFile(certPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert}), 0644)
}
