# 新聞前端（Astro SSR）。兩段式：建置需要 tailwind／typescript，執行期不需要。
FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0 PORT=4321
COPY package.json package-lock.json ./
# dist 不是完全獨立的 bundle，執行時仍需要 astro 與 @astrojs/node
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
USER node
EXPOSE 4321
CMD ["node", "./dist/server/entry.mjs"]
