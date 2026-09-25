FROM public.ecr.aws/docker/library/python:3.11.11-slim-bookworm@sha256:081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts scripts
COPY .build/gateway-admin /usr/local/bin/gateway-admin
EXPOSE 8443
ENTRYPOINT ["gateway-admin"]
