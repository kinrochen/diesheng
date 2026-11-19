FROM python:3.12-slim-bookworm
RUN groupadd -g 10001 chat && useradd -r -u 10001 -g chat chat && install -d -o chat -g chat -m 0700 /data
COPY relay.pyz /opt/relay.pyz
USER chat
ENTRYPOINT ["python3","-I","/opt/relay.pyz","--data-dir","/data/workspace","serve"]
