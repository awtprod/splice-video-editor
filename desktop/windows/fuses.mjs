// Electron fuses for Splice.exe, set at build time (build.py runs this after staging).
//
//   node fuses.mjs harden <Splice.exe>     turn the fuses below off, then read them back
//   node fuses.mjs check <Splice.exe>      fail unless they are all off
//   node fuses.mjs test-copy <copy.exe>    a copy for Playwright only: --inspect back on
//
// Off, Splice.exe can no longer be used as a plain Node.js runtime: ELECTRON_RUN_AS_NODE,
// NODE_OPTIONS and --inspect/--inspect-brk are ignored. Run `npm ci` in this folder
// first; @electron/fuses is a build-time dependency only and is not shipped.
import { FuseState, FuseV1Options, FuseVersion, flipFuses, getCurrentFuseWire } from '@electron/fuses'
import { basename } from 'node:path'

const HARDENED = ['RunAsNode', 'EnableNodeOptionsEnvironmentVariable', 'EnableNodeCliInspectArguments']
const NAMES = { [FuseState.DISABLE]: 'disabled', [FuseState.ENABLE]: 'enabled', [FuseState.REMOVED]: 'removed' }

async function states(executable) {
  const wire = await getCurrentFuseWire(executable)
  return Object.fromEntries(HARDENED.map(name => [name, NAMES[wire[FuseV1Options[name]]] || 'unknown']))
}

async function main(mode, executable) {
  if (!executable) throw new Error('usage: node fuses.mjs harden|check|test-copy <electron executable>')
  if (mode === 'harden') {
    await flipFuses(executable, {
      version: FuseVersion.V1,
      ...Object.fromEntries(HARDENED.map(name => [FuseV1Options[name], false]))
    })
  } else if (mode === 'test-copy') {
    // Playwright drives Electron through the Node inspector. Never on the shipped file.
    if (basename(executable).toLowerCase() === 'splice.exe' || basename(executable) === 'Splice') {
      throw new Error('test-copy is for a copy of Splice.exe, not Splice.exe itself')
    }
    await flipFuses(executable, { version: FuseVersion.V1, [FuseV1Options.EnableNodeCliInspectArguments]: true })
    console.log(JSON.stringify(await states(executable)))
    return
  } else if (mode !== 'check') {
    throw new Error(`unknown mode ${mode}`)
  }
  const result = await states(executable)
  const open = HARDENED.filter(name => result[name] !== 'disabled')
  console.log(JSON.stringify(result))
  if (open.length) throw new Error(`${executable}: these fuses are not off: ${open.join(', ')}`)
}

main(...process.argv.slice(2)).catch(error => {
  console.error(error.message)
  process.exitCode = 1
})
