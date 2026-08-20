// herdr-remote service worker — Web Push 通知 + 应用外壳缓存

// 版本号必须随资源改动递增：缓存键换名后旧缓存会在 activate 阶段被清掉，
// 否则改版后用户会一直吃到旧的 index.html。
const CACHE = 'herdr-shell-v2';

// 应用外壳：index.html 是单文件应用，装上它 + 图标就能离线打开。
// 不预缓存 esm.sh 的第三方模块——它只提供音效，且跨域请求失败会让
// 整个 addAll 事务回滚，导致什么都缓存不上。
const SHELL = ['./', './index.html', './logo.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      // 首装时若某个资源取不到，不能让 SW 安装失败——
      // 那会连推送功能一起丢掉。缓存是增强，不是前提。
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // 只处理 GET。POST/PUT 等写操作缓存了会造成重复提交。
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // 只接管本源资源。relay 的 WebSocket 与 API 必须始终走网络，
  // 缓存了会拿到过期的 agent 状态——这比慢更糟。
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  // 外壳走 stale-while-revalidate：立刻返回缓存让 PWA 秒开，
  // 同时后台拉新版本供下次使用。导航请求（打开 App）也走这条，
  // 于是离线时仍能打开界面，只是连不上 relay。
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => cached);   // 离线且无缓存时返回 undefined，由浏览器兜底报错
      return cached || network;
    })
  );
});

self.addEventListener('push', (event) => {
  let data = { title: '🐑 herdr', body: 'Agent needs attention', url: '/' };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (e) {}
  // Clear notification (sent when agent unblocks)
  if (data.type === 'clear') {
    event.waitUntil(
      self.registration.getNotifications({ tag: data.tag || 'herdr-blocked' }).then((notes) => {
        notes.forEach((n) => n.close());
      })
    );
    return;
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/logo.svg',
      badge: '/logo.svg',
      tag: 'herdr-blocked',
      renotify: true,
      data: { url: data.url },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(self.location.origin)) {
          client.focus();
          client.postMessage({ type: 'navigate', url });
          return;
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
