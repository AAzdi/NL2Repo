ARG PYTHON_VERSION=3.12
FROM node:22-bookworm AS node
FROM python:${PYTHON_VERSION}-bookworm
COPY --from=node /usr/local/ /usr/local/
ARG CLAUDE_CODE_VERSION
RUN test -n "$CLAUDE_CODE_VERSION" && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
ENV HOME=/tmp/claude-home
ENV PATH=/tmp/claude-home/.local/bin:$PATH
ENV PIP_BREAK_SYSTEM_PACKAGES=1
WORKDIR /workspace
USER 1000:1000
ENTRYPOINT ["sh", "-c", "mkdir -p \"$HOME\" && exec claude \"$@\"", "claude"]
