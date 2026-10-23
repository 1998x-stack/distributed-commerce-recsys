// Explicitly expose public commerce paths; never proxy /api/admin/* to browsers.
// Next.js evaluates rewrites at build time: Dockerfile must pass API_INTERNAL_URL.
const api = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';
const publicPaths = [
  '/api/products', '/api/products/:path*', '/api/search', '/api/categories',
  '/api/events', '/api/events/batch', '/api/cart', '/api/cart/:path*',
  '/api/recommendations/:path*', '/api/health',
];
export default {
  async rewrites() {
    return publicPaths.map(source => ({source, destination: `${api}${source}`}));
  },
};
