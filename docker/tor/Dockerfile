# Local development/testing Tor instance for the experiment/tor-proxy-test
# branch. Not built or referenced by the default docker-compose.yml or by
# docker-compose.prod.yml -- only docker-compose.tor.yml uses this image.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tor netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY torrc.base /etc/tor/torrc.base
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 9050 9051

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
