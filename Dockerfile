FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./astra/pyproject.toml /astra/pyproject.toml
COPY ./astra/uv.lock /astra/uv.lock
WORKDIR /astra
RUN uv sync --frozen --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./astra /astra
RUN uv sync --frozen -i https://mirrors.aliyun.com/pypi/simple/

ENV TZ=Asia/Shanghai