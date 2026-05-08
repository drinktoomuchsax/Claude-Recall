export const STATE_NAMES: Record<number, string> = {
  0: 'off',
  10: 'idle',
  30: 'working',
  40: 'tool_active',
  60: 'awaiting_input',
  80: 'awaiting_permission',
  85: 'notification',
  100: 'error',
}

export const STATE_DISPLAY: Record<string, string> = {
  off: 'Offline',
  idle: 'Idle',
  working: 'Thinking',
  tool_active: 'Running tool',
  awaiting_input: 'Waiting for you',
  awaiting_permission: 'Needs permission',
  notification: 'Has a message',
  error: 'Error',
}

export interface SessionMetadata {
  cwd?: string
  project?: string
  model?: string
  prompt?: string
  tool_name?: string
  tool_context?: string
  effort_level?: string
  agent_id?: string
  agent_type?: string
  error_type?: string
}

export interface StateDurations {
  off: number
  idle: number
  working: number
  tool_active: number
  awaiting_input: number
  awaiting_permission: number
  notification: number
  error: number
}

export interface StateHistoryEntry {
  state: string
  timestamp: Date
}

export interface SessionState {
  id: string
  state: string
  previousState: string
  lastChange: Date
  eventCount: number
  metadata?: SessionMetadata
  duration?: number
  durations?: StateDurations
  history: StateHistoryEntry[]
}

export interface AggregateState {
  state: string
  activeSessions: number
  breakdown: Record<string, number>
}

export const STATE_COLORS: Record<string, string> = {
  off: '#333333',
  idle: '#2d5a3a',
  working: '#1e90ff',
  tool_active: '#3b82f6',
  awaiting_input: '#f59e0b',
  awaiting_permission: '#a855f7',
  notification: '#c084fc',
  error: '#ef4444',
}

export const MODEL_LABELS: Record<string, { short: string; color: string }> = {
  opus: { short: 'Opus', color: '#e8a838' },
  sonnet: { short: 'Sonnet', color: '#6ea8fe' },
  haiku: { short: 'Haiku', color: '#66d9a0' },
}

export function parseModel(model?: string): { short: string; color: string } | null {
  if (!model) return null
  const lower = model.toLowerCase()
  for (const [key, val] of Object.entries(MODEL_LABELS)) {
    if (lower.includes(key)) return val
  }
  return { short: model.split('-').slice(0, 2).join(' '), color: '#888' }
}

export const EFFORT_COLORS: Record<string, string> = {
  low: '#555',
  medium: '#888',
  high: '#f59e0b',
  xhigh: '#ef8b2e',
  max: '#ef4444',
}
