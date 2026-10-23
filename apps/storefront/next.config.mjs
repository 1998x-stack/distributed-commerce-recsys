const api = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';
export default {
  async rewrites() {
    return [{source: '/api/:path*', destination: `${api}/api/:path*`},
            {source: '/health/:path*', destination: `${api}/health/:path*`}];
  },
};
