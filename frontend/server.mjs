import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize } from 'node:path'

const port = Number(process.env.PORT || 8501)
const root = join(process.cwd(), 'dist')
const backend = (process.env.BACKEND_URL || '').replace(/\/$/, '')
const projectId = process.env.PROJECT_ID || ''
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json; charset=utf-8' }

createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://localhost')
  if (url.pathname === '/app-config.js') {
    res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8', 'cache-control': 'no-store' })
    res.end(`window.__APP_CONFIG__=${JSON.stringify({ projectId })};`)
    return
  }
  if (url.pathname === '/healthz') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end('{"status":"ok"}')
    return
  }
  if ((url.pathname === '/health' || url.pathname.startsWith('/api/')) && backend) {
    try {
      const headers = new Headers()
      for (const [key, value] of Object.entries(req.headers)) if (value && !['host', 'content-length'].includes(key)) headers.set(key, Array.isArray(value) ? value.join(', ') : value)
      const body = ['GET', 'HEAD'].includes(req.method || 'GET') ? undefined : await new Promise((resolve, reject) => {
        const chunks = []; req.on('data', chunk => chunks.push(chunk)); req.on('end', () => resolve(Buffer.concat(chunks))); req.on('error', reject)
      })
      const upstream = await fetch(`${backend}${url.pathname}${url.search}`, { method: req.method, headers, body })
      const responseHeaders = Object.fromEntries(upstream.headers.entries())
      delete responseHeaders['content-encoding']; delete responseHeaders['content-length']
      res.writeHead(upstream.status, responseHeaders)
      res.end(Buffer.from(await upstream.arrayBuffer()))
    } catch (error) {
      res.writeHead(502, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ detail: `Backend unavailable: ${error.message}` }))
    }
    return
  }
  const requested = normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, '')
  let file = join(root, requested === '/' ? 'index.html' : requested)
  if (!file.startsWith(root) || !existsSync(file) || statSync(file).isDirectory()) file = join(root, 'index.html')
  res.writeHead(200, { 'content-type': types[extname(file)] || 'application/octet-stream' })
  createReadStream(file).pipe(res)
}).listen(port, '0.0.0.0', () => console.log(`Change Coordinator UI listening on ${port}`))
