import { useEffect, useRef, useState, useCallback } from 'react'
import { SessionState, SessionMetadata, AggregateState, STATE_NAMES } from './types'

const WS_URL = 'ws://127.0.0.1:8765/ws?mode=all'
const API_BASE = 'http://127.0.0.1:8765'

function resolveState(s: number | string): string {
  if (typeof s === 'number') return STATE_NAMES[s] ?? 'off'
  return s
}

export function useRecall() {
  const [sessions, setSessions] = useState<Record<string, SessionState>>({})
  const [aggregate, setAggregate] = useState<AggregateState>({
    state: 'off',
    activeSessions: 0,
    breakdown: {},
  })
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>()

  const connect = useCallback(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      // Fetch initial state
      fetch(`${API_BASE}/sessions`)
        .then(r => r.json())
        .then(data => {
          const initial: Record<string, SessionState> = {}
          for (const [id, entry] of Object.entries(data.sessions ?? {})) {
            const info = entry as { state: string; metadata?: SessionMetadata }
            initial[id] = {
              id,
              state: info.state,
              previousState: 'off',
              lastChange: new Date(),
              eventCount: 0,
              metadata: info.metadata,
              history: [{ state: info.state, timestamp: new Date() }],
            }
          }
          setSessions(initial)
        })
        .catch(() => {})

      fetch(`${API_BASE}/state`)
        .then(r => r.json())
        .then(data => {
          setAggregate({
            state: data.state,
            activeSessions: data.active_sessions,
            breakdown: data.breakdown ?? {},
          })
        })
        .catch(() => {})
    }

    ws.onmessage = (event) => {
      const frame = JSON.parse(event.data)

      if (frame.type === 'aggregate') {
        setAggregate({
          state: resolveState(frame.state),
          activeSessions: frame.active_sessions,
          breakdown: frame.breakdown ?? {},
        })
      } else if (frame.type === 'session') {
        const state = resolveState(frame.state)
        const previousState = resolveState(frame.previous)
        const sid = frame.session_id

        if (state === 'off') {
          setSessions(curr => {
            const next = { ...curr }
            delete next[sid]
            return next
          })
        } else {
          setSessions(curr => {
            const prev = curr[sid]
            const history = [...(prev?.history ?? []), { state, timestamp: new Date(frame.timestamp) }]
            return {
              ...curr,
              [sid]: {
                id: sid,
                state,
                previousState: prev?.state ?? previousState,
                lastChange: new Date(frame.timestamp),
                eventCount: (prev?.eventCount ?? 0) + 1,
                metadata: frame.metadata ?? prev?.metadata,
                duration: frame.duration,
                durations: frame.durations ?? prev?.durations,
                history,
              },
            }
          })
        }
      }
    }

    ws.onclose = () => {
      setConnected(false)
      reconnectRef.current = setTimeout(connect, 2000)
    }

    ws.onerror = () => ws.close()
  }, [])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
    }
  }, [connect])

  return { sessions, aggregate, connected }
}
