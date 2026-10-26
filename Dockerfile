ARG BASE_IMAGE=python:3.12-slim
ARG APT_MIRROR=deb.debian.org
ARG LIBREDWG_URL=https://ftp.gnu.org/gnu/libredwg/libredwg-0.14.tar.gz
ARG PIP_INDEX_URL=https://pypi.org/simple

FROM ${BASE_IMAGE} AS libredwg
ARG APT_MIRROR
ARG LIBREDWG_URL
USER root
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl pkg-config \
        libxml2-dev libpcre2-dev libyaml-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "${LIBREDWG_URL}" \
        -o /tmp/libredwg.tar.gz \
    && mkdir -p /tmp/libredwg \
    && tar -xzf /tmp/libredwg.tar.gz -C /tmp/libredwg --strip-components=1 \
    && cd /tmp/libredwg \
    && ./configure --disable-docs --disable-python \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig

FROM ${BASE_IMAGE}
ARG APT_MIRROR
ARG PIP_INDEX_URL
ARG HVAC_VERSION=0.1.0
ARG HVAC_COMMIT=unknown
LABEL org.opencontainers.image.title="hvac-quote" \
      org.opencontainers.image.version="${HVAC_VERSION}" \
      org.opencontainers.image.revision="${HVAC_COMMIT}" \
      org.opencontainers.image.source="https://github.com/supeng0916-beep/hvac-quote"
USER root
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    HVAC_DATA_DIR=/data \
    HVAC_RUNS_DIR=/data/runs \
    HVAC_DEFAULT_TEMPLATE=/data/default-template.xlsx \
    PORT=8000
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libreoffice-calc \
        fonts-noto-cjk \
        fonts-wqy-microhei \
        libxml2 \
        libpcre2-8-0 \
        libyaml-0-2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=libredwg /usr/local/bin/dwg2dxf /usr/local/bin/dwg2dxf
COPY --from=libredwg /usr/local/lib/libredwg* /usr/local/lib/
RUN ldconfig \
    && dwg2dxf --version \
    && soffice --headless --version | head -1
WORKDIR /app
COPY pyproject.toml uv.lock requirements.lock.txt ./
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.lock.txt
COPY src ./src
RUN pip install --no-cache-dir --no-deps --index-url "${PIP_INDEX_URL}" .
RUN python -c "from hvac_quote.vocabulary import VOCABULARY; assert VOCABULARY"
COPY docker/entrypoint.sh /usr/local/bin/hvac-entrypoint
RUN chmod 0755 /usr/local/bin/hvac-entrypoint
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
ENTRYPOINT ["hvac-entrypoint"]
