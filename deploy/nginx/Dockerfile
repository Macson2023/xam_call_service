FROM nginx:1.27-alpine

RUN apk add --no-cache openssl

COPY 10-self-signed-cert.sh /docker-entrypoint.d/10-self-signed-cert.sh
RUN chmod +x /docker-entrypoint.d/10-self-signed-cert.sh \
    && rm -f /etc/nginx/conf.d/default.conf

COPY calls.conf.template /etc/nginx/templates/calls.conf.template
