ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY app /app
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099
CMD ["/run.sh"]
