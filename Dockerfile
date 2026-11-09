FROM node:22-bookworm-slim AS node
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY src ./src
COPY public ./public

FROM python:3.12-slim AS py
WORKDIR /app/python
COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY python/ ./

FROM node:22-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*
COPY --from=node /app /app
COPY --from=py /app/python /app/python
RUN pip3 install --break-system-packages --no-cache-dir -r /app/python/requirements.txt \
  || pip3 install --no-cache-dir -r /app/python/requirements.txt
ENV PORT=8787
ENV PERCOLATOR_PY_MCP_URL=http://127.0.0.1:8791/mcp
EXPOSE 8787
COPY scripts/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
