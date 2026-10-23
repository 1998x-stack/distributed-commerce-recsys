FROM node:22-alpine
WORKDIR /app
COPY apps/storefront/package.json apps/storefront/package.json
WORKDIR /app/apps/storefront
RUN npm install --no-audit --no-fund
COPY apps/storefront ./
ARG API_INTERNAL_URL=http://api:8000
ENV API_INTERNAL_URL=${API_INTERNAL_URL}
RUN npm run build
EXPOSE 3000
CMD ["npm", "run", "start"]
