import { Box, Text, useInput } from '@hermes/ink'
import type { ConnectionOperationTarget, ConnectionRespondParams, ConnectionTargetEnvField } from '@hermes/shared/gateway-events'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { $connectionOperation, clearConnectionOperation } from '../app/connectionOperationStore.js'
import { useGateway } from '../app/gatewayContext.js'
import { $uiSessionId } from '../app/uiStore.js'
import { openExternalUrl } from '../lib/openExternalUrl.js'
import type { Theme } from '../theme.js'

import { TextInput } from './textInput.js'

interface ConnectionSetupOverlayProps {
  cols: number
  t: Theme
}

interface FormKeyHandlers {
  cancel: () => void
  connect: () => void
  fieldCount: number
  selectorFocused: boolean
  setAction: (update: (value: 0 | 1) => 0 | 1) => void
  setFocus: (update: (value: number) => number) => void
  submitting: boolean
}

interface InputKey {
  downArrow: boolean
  escape: boolean
  leftArrow: boolean
  return: boolean
  rightArrow: boolean
  shift: boolean
  tab: boolean
  upArrow: boolean
}

const isUnresolved = (target: ConnectionOperationTarget): boolean =>
  !['connected', 'skipped', 'expired', 'unavailable'].includes(target.state) || Boolean(target.discovery_error)

const initialDraft = (fields: ConnectionTargetEnvField[]): Record<string, string> =>
  Object.fromEntries(fields.map(field => [field.name, field.secret ? '' : field.default]))

const fieldLabel = (field: ConnectionTargetEnvField): string => field.prompt || field.name

const isAuthorizedWithoutTools = (target: ConnectionOperationTarget | null): boolean =>
  target?.state === 'connected' && Boolean(target.discovery_error)

const isAwaitingBrowser = (target: ConnectionOperationTarget | null): boolean =>
  target?.state === 'initiated' && Boolean(target.connect_url)

const isAppBasedActionable = (target: ConnectionOperationTarget): boolean =>
  target.kind === 'app_based_mcp' && ['pending', 'failed', 'expired'].includes(target.state)

const isAppBasedFocusable = (target: ConnectionOperationTarget): boolean =>
  !['connected', 'skipped', 'unavailable'].includes(target.state)

const appBasedOpenSupported = (target: ConnectionOperationTarget): boolean => Boolean(target.open_supported)

/** Key routing while the field list and the Connect/Cancel selector are on screen. */
function handleFormKey(key: InputKey, h: FormKeyHandlers): void {
  const rows = h.fieldCount + 1
  const back = (value: number) => (value - 1 + rows) % rows
  const forward = (value: number) => (value + 1) % rows

  if (h.submitting) {
    return
  }

  if (key.shift && key.tab) {
    h.setFocus(back)
  } else if (key.tab || (key.downArrow && !h.selectorFocused)) {
    h.setFocus(forward)
  } else if (key.upArrow && !h.selectorFocused) {
    h.setFocus(back)
  } else if (h.selectorFocused && (key.leftArrow || key.rightArrow || key.upArrow || key.downArrow)) {
    h.setAction(value => (value === 0 ? 1 : 0))
  } else if (h.selectorFocused && key.return) {
    h.connect()
  }
}

function AuthorizedWithoutTools({ error, t }: { error: string; t: Theme }) {
  return (
    <Box flexDirection="column">
      <Text bold color={t.color.ok}>Authorized. Tools unavailable.</Text>
      <Text color={t.color.muted}>{error}</Text>
      <Text color={t.color.accent}>▸ Continue</Text>
      <Text color={t.color.muted}>Esc close</Text>
    </Box>
  )
}

function AwaitingBrowser({ t, target }: { t: Theme; target: ConnectionOperationTarget }) {
  return (
    <Box flexDirection="column">
      <Text bold color={t.color.text}>Set up {target.name}</Text>
      <Text color={t.color.accent}>{target.connect_url}</Text>
      {target.detail ? <Text color={t.color.muted}>{target.detail}</Text> : null}
      <Text color={t.color.muted}>Press Enter to open in browser</Text>
    </Box>
  )
}

interface FieldRowProps {
  cols: number
  draftValue: string
  field: ConnectionTargetEnvField
  focused: boolean
  onChange: (value: string) => void
  onSubmit: () => void
  showSet: boolean
  submitting: boolean
  t: Theme
}

function FieldRow({ cols, draftValue, field, focused, onChange, onSubmit, showSet, submitting, t }: FieldRowProps) {
  return (
    <Box flexDirection="column">
      <Text color={focused ? t.color.accent : t.color.label}>
        {focused ? '▸ ' : '  '}{fieldLabel(field)}{field.required ? ' *' : ''}
      </Text>
      <Box paddingLeft={2}>
        {showSet ? (
          <Text color={t.color.ok}>Set</Text>
        ) : (
          <TextInput
            color={t.color.text}
            columns={Math.max(20, cols - 8)}
            focus={!submitting && focused}
            mask={field.secret ? '*' : undefined}
            onChange={onChange}
            onSubmit={onSubmit}
            value={draftValue}
          />
        )}
      </Box>
    </Box>
  )
}

interface AppBasedMcpRowsProps {
  focusedName: string | undefined
  t: Theme
  targets: ConnectionOperationTarget[]
}

function AppBasedMcpRows({ focusedName, t, targets }: AppBasedMcpRowsProps) {
  return (
    <Box flexDirection="column">
      {targets.map(item => {
        const focused = item.name === focusedName
        const color = item.state === 'connected'
          ? t.color.ok
          : item.state === 'failed'
            ? t.color.error
            : item.state === 'initiated' || item.state === 'unavailable'
              ? t.color.muted
              : focused
                ? t.color.accent
                : t.color.muted

        return (
          <Box flexDirection="column" key={item.name}>
            <Text bold={focused} color={color}>
              {focused ? '▸ ' : '  '}{item.name}
            </Text>
            {item.detail ? <Text color={color} wrap="wrap">  {item.detail}</Text> : null}
          </Box>
        )
      })}
    </Box>
  )
}

interface SetupFormProps {
  action: 0 | 1
  cols: number
  draft: Record<string, string>
  fields: ConnectionTargetEnvField[]
  focus: number
  missingRequired: ConnectionTargetEnvField | undefined
  onChange: (name: string, value: string) => void
  onFieldSubmit: (index: number) => void
  selectorFocused: boolean
  submittedSecrets: Set<string>
  submitting: boolean
  t: Theme
  target: ConnectionOperationTarget
}

function SetupForm(p: SetupFormProps) {
  const { t, target } = p

  return (
    <Box flexDirection="column">
      <Text bold color={t.color.text}>Set up {target.name}</Text>
      {target.instructions ? <Text color={t.color.muted} wrap="wrap">{target.instructions}</Text> : null}
      {p.fields.map((field, index) => (
        <FieldRow
          cols={p.cols}
          draftValue={p.draft[field.name] ?? ''}
          field={field}
          focused={p.focus === index}
          key={field.name}
          onChange={value => p.onChange(field.name, value)}
          onSubmit={() => p.onFieldSubmit(index)}
          showSet={p.submittedSecrets.has(field.name) && p.submitting}
          submitting={p.submitting}
          t={t}
        />
      ))}
      {target.state === 'failed' && target.detail ? <Text color={t.color.error}>{target.detail}</Text> : null}
      <Text color={p.selectorFocused ? t.color.accent : t.color.muted}>
        {p.action === 0 ? '▸ ' : '  '}Connect   {p.action === 1 ? '▸ ' : '  '}Cancel
      </Text>
      {p.missingRequired ? <Text color={t.color.muted}>{fieldLabel(p.missingRequired)} is required.</Text> : null}
      {p.submitting ? <Text color={t.color.muted}>Pending…</Text> : null}
      <Text color={t.color.muted}>↑/↓ or Tab move · ←/→ select · Enter confirm · Esc cancel</Text>
    </Box>
  )
}

// The component owns one draft across backend snapshots; splitting it would remount and erase failed submissions.
export function ConnectionSetupOverlay({ cols, t }: ConnectionSetupOverlayProps) {
  const operation = useStore($connectionOperation)
  const sid = useStore($uiSessionId)
  const { gw } = useGateway()
  const appBasedTargets = operation?.targets.filter(item => item.kind === 'app_based_mcp') ?? []
  const focusableAppBasedTargets = appBasedTargets.filter(isAppBasedFocusable)
  const [appBasedFocus, setAppBasedFocus] = useState(0)
  const appBasedTarget = focusableAppBasedTargets[appBasedFocus] ?? null
  const target = appBasedTargets.length > 0 ? appBasedTarget : operation?.targets.find(isUnresolved) ?? null
  const fields = useMemo<ConnectionTargetEnvField[]>(() => target?.required_env ?? [], [target?.required_env])
  const targetKey = `${operation?.opId ?? ''}:${target?.name ?? ''}`
  const [draft, setDraft] = useState<Record<string, string>>(() => initialDraft(fields))
  const [focus, setFocus] = useState(0)
  const [action, setAction] = useState<0 | 1>(0)
  const [submitting, setSubmitting] = useState(false)
  const [submittedSecrets, setSubmittedSecrets] = useState<Set<string>>(() => new Set())

  // A new target starts clean. Every backend snapshot carries a freshly parsed required_env, so the
  // same target's fields only fill in what the draft lacks: a failed Connect keeps what was typed.
  useEffect(() => {
    setDraft({})
    setFocus(0)
    setAction(0)
    setSubmitting(false)
    setSubmittedSecrets(new Set())
  }, [targetKey])

  useEffect(() => {
    setAppBasedFocus(current => Math.min(current, Math.max(0, focusableAppBasedTargets.length - 1)))
  }, [focusableAppBasedTargets.length])

  useEffect(() => {
    setDraft(current => ({ ...initialDraft(fields), ...current }))
  }, [fields])

  useEffect(() => {
    if (target && ['failed', 'expired', 'pending'].includes(target.state)) {
      setSubmitting(false)
    }

    if (target?.state === 'connected') {
      setDraft(current =>
        Object.fromEntries(fields.map(field => [field.name, field.secret ? '' : current[field.name] ?? '']))
      )
    }
  }, [fields, target?.state])

  useEffect(() => {
    if (operation && operation.targets.every(item => !isUnresolved(item))) {
      clearConnectionOperation()
    }
  }, [operation])

  const missingRequired = fields.find(field => field.required && !draft[field.name]?.trim())
  const selectorFocused = focus === fields.length

  const respond = (result: ConnectionRespondParams['result']) => {
    if (!operation || !sid || submitting) {
      return
    }

    setSubmitting(true)
    void gw
      .request('connection.respond', { op_id: operation.opId, result, session_id: sid })
      .catch(() => setSubmitting(false))
  }

  const settleOnKey = (key: InputKey): boolean => {
    if (isAuthorizedWithoutTools(target)) {
      if (key.escape || key.return) {
        respond({ settled_by: 'continue' })
      }

      return true
    }

    if (isAwaitingBrowser(target)) {
      if (key.return && target?.connect_url) {
        openExternalUrl(target.connect_url)
      }

      return !key.escape
    }

    return false
  }

  const cancel = () => {
    if (!target) {
      clearConnectionOperation()

      return
    }

    respond({ targets: [{ name: target.name, status: 'skipped' }] })
  }

  const connect = () => {
    if (!target || missingRequired) {
      const index = missingRequired ? fields.indexOf(missingRequired) : 0
      setFocus(index < 0 ? 0 : index)

      return
    }

    setSubmittedSecrets(new Set(fields.filter(field => field.secret).map(field => field.name)))
    respond({ targets: [{ env: draft, name: target.name, status: 'approved' }] })
  }

  const actOnAppBasedTarget = () => {
    if (!target || !isAppBasedActionable(target)) {
      return
    }

    respond({
      targets: [{
        name: target.name,
        status: appBasedOpenSupported(target) ? 'open' : 'approved'
      }]
    })
  }

  // A single input owner guarantees Esc and navigation cause exactly one action.
  useInput((ch, key) => {
    if (appBasedTargets.length > 0) {
      if (!target) {
        if (key.escape || key.return) {
          respond({ settled_by: 'continue' })
        }
      } else if (key.escape) {
        cancel()
      } else if (focusableAppBasedTargets.length > 1 && (key.upArrow || key.downArrow || key.tab)) {
        const direction = key.upArrow || (key.shift && key.tab) ? -1 : 1
        setAppBasedFocus(current => (
          (current + direction + focusableAppBasedTargets.length) % focusableAppBasedTargets.length
        ))
      } else if (!submitting && isAppBasedActionable(target) && (
        key.return || (appBasedOpenSupported(target) && ch.toLowerCase() === 'o')
      )) {
        actOnAppBasedTarget()
      }

      return
    }

    if (settleOnKey(key)) {
      return
    }

    if (key.escape) {
      cancel()

      return
    }

    handleFormKey(key, {
      cancel,
      connect: () => (action === 0 ? connect() : cancel()),
      fieldCount: fields.length,
      selectorFocused,
      setAction,
      setFocus,
      submitting
    })
  })

  if (!operation || (appBasedTargets.length === 0 && !target)) {
    return null
  }

  if (appBasedTargets.length > 0) {
    const actionLabel = target && appBasedOpenSupported(target) ? 'Open' : 'Connect'

    return (
      <Box flexDirection="column">
        <AppBasedMcpRows focusedName={target?.name} t={t} targets={appBasedTargets} />
        <Text color={t.color.muted}>
          {target
            ? isAppBasedActionable(target)
              ? `Enter ${actionLabel} · Esc Skip`
              : 'Esc Skip'
            : 'Enter Continue'}
        </Text>
      </Box>
    )
  }

  if (!target) {
    return null
  }

  if (isAuthorizedWithoutTools(target)) {
    return <AuthorizedWithoutTools error={target.discovery_error ?? ''} t={t} />
  }

  if (isAwaitingBrowser(target)) {
    return <AwaitingBrowser t={t} target={target} />
  }

  return (
    <SetupForm
      action={action}
      cols={cols}
      draft={draft}
      fields={fields}
      focus={focus}
      missingRequired={missingRequired}
      onChange={(name, value) => setDraft(current => ({ ...current, [name]: value }))}
      onFieldSubmit={index => setFocus(index === fields.length - 1 ? fields.length : index + 1)}
      selectorFocused={selectorFocused}
      submittedSecrets={submittedSecrets}
      submitting={submitting}
      t={t}
      target={target}
    />
  )
}
