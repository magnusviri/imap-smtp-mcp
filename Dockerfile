FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 mailmcp
RUN mkdir /state && chown mailmcp:mailmcp /state
USER mailmcp

ENV PERSONAL_MAIL_MCP_HOST=0.0.0.0 \
    PERSONAL_MAIL_MCP_PORT=8787 \
    PERSONAL_MAIL_MCP_ALLOW_CONTAINER_BIND=1 \
    PERSONAL_MAIL_MCP_SECRETS_DIR=/run/secrets
EXPOSE 8787
ENTRYPOINT ["personal-mail-mcp"]
