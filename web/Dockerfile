# Build stage
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY public ./public
COPY src ./src
RUN npm run build

# Runtime stage: static files behind nginx, as a non-root user, on port 8080.
FROM nginx:alpine
# The official image renders /etc/nginx/templates/*.template at start, but it
# writes to /etc/nginx/conf.d, /var/cache/nginx and /var/run, which the
# non-root user must own. Substitute only our three variables.
ENV API_UPSTREAM=http://concierge-api:8080 \
    OTLP_UPSTREAM=http://otel-collector:4318 \
    GATE_UPSTREAM=http://127.0.0.1:8090 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER='^((API|OTLP|GATE)_UPSTREAM|NGINX_LOCAL_RESOLVERS)$'
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
RUN sed -i 's#^pid .*#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf \
 && chown -R nginx:nginx /etc/nginx/conf.d /var/cache/nginx /usr/share/nginx/html
USER nginx
EXPOSE 8080 8081
