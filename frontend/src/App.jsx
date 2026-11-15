import { useState, useEffect } from 'react'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

function App() {
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const checkHealth = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/api/health`)
      if (!res.ok) {
        throw new Error(`HTTP error! status: ${res.status}`)
      }
      const data = await res.json()
      setHealth(data)
    } catch (err) {
      setError(err.message || 'Failed to connect to SatView API')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    checkHealth()
  }, [])

  return (
    <div className="container">
      <header>
        <span className="badge">SatView · Phase 0</span>
        <h1>SatView</h1>
        <p style={{ color: '#94a3b8', margin: '0.25rem 0 1.5rem' }}>
          Satellite tracking and open-imagery discovery platform
        </p>
      </header>

      <main>
        <div className="card">
          <h2>System Foundation Status</h2>
          <p style={{ color: '#cbd5e1' }}>
            Phase 0 verification: API service, SQLite database, and local Ollama connection.
          </p>

          {loading && <p>Checking backend health...</p>}

          {error && (
            <div style={{ marginTop: '1rem', color: '#f87171' }}>
              <strong>API Connection Notice:</strong> {error}
              <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                Ensure FastAPI is running: <code>uvicorn app.main:app --reload</code>
              </p>
            </div>
          )}

          {health && (
            <div className="status-grid">
              <div className="status-item">
                <div className="status-label">Backend Status</div>
                <div className="status-val" style={{ color: '#34d399' }}>
                  {health.status.toUpperCase()}
                </div>
              </div>
              <div className="status-item">
                <div className="status-label">Database</div>
                <div className="status-val">
                  {health.database.type} ({health.database.status})
                </div>
              </div>
              <div className="status-item">
                <div className="status-label">Ollama Engine</div>
                <div className="status-val">
                  {health.ollama.status}
                </div>
                <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginTop: '0.25rem' }}>
                  Model: {health.ollama.configured_model} (
                  {health.ollama.model_available ? 'Ready' : 'Not Found'})
                </div>
              </div>
            </div>
          )}

          <button onClick={checkHealth} disabled={loading}>
            {loading ? 'Checking...' : 'Re-check Status'}
          </button>
        </div>
      </main>
    </div>
  )
}

export default App
