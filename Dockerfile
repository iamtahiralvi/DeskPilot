# For MCP registries' automated checks (e.g. Glama). DeskPilot controls a *Windows* desktop, so inside this Linux
# container the server starts and lists its tools, but every tool replies that it only runs on Windows.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["deskpilot", "serve"]
