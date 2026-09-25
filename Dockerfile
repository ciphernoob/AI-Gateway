FROM ghcr.io/neomantra/openresty:1.29.2.4-1-bookworm-fat@sha256:304c0b1531e9bbe7521b54cd8eb1ce986ca61075ab8fb5c09dee95657c37244c
WORKDIR /app
COPY lua /app/lua
COPY redis /app/redis
COPY nginx /app/nginx
COPY tests/lua /app/tests/lua
RUN mkdir -p /app/run /app/logs
EXPOSE 8080
CMD ["openresty", "-p", "/app/", "-c", "nginx/nginx.conf", "-g", "daemon off;"]
