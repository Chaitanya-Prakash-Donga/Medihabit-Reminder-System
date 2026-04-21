self.addEventListener('install', (e) => {
  console.log('[Service Worker] Install');
});

self.addEventListener('fetch', (e) => {
  // This allows the app to serve assets from cache if needed
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
