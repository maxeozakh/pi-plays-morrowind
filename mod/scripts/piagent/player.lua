-- pi plays Morrowind: the in-game half of the harness.
--
-- The sidecar (game.py) writes one command at a time to the VFS file
-- piagent/cmd.txt as a Lua table literal; this script polls it, runs the
-- command over as many frames as it needs, then prints the result as one
-- `PIAGENT {json}` line to openmw.log for the sidecar.
--
-- By default the world keeps running between commands. Commands sent with
-- pause=true leave it paused (tag "piagent") afterwards, so the model can
-- think without anything happening in the game.
--
-- The model plays from screenshots alone. Ops therefore only do what a player
-- could do with the keyboard and mouse, and never tell the model anything it
-- couldn't see on screen. The state snapshot in each reply is for the harness
-- (run logs, milestones, the human web UI) and must not reach the model.
local camera = require('openmw.camera')
local core = require('openmw.core')
local self = require('openmw.self')
local types = require('openmw.types')
local util = require('openmw.util')
local vfs = require('openmw.vfs')
local I = require('openmw.interfaces')

local json = require('scripts.piagent.json')

local CMD_FILE = 'piagent/cmd.txt'
local POLL_INTERVAL = 0.05

local Actor = types.Actor
local Player = types.Player

local function round(x, step)
    step = step or 1
    return math.floor(x / step + 0.5) * step
end

local function normAngle(a)
    while a > math.pi do a = a - 2 * math.pi end
    while a < -math.pi do a = a + 2 * math.pi end
    return a
end

local function try(fn, ...)
    local ok, res = pcall(fn, ...)
    if ok then return res end
    return nil
end

local function objectName(obj)
    local rec = try(function() return obj.type.record(obj) end)
    local name = rec and rec.name
    if name and name ~= '' then return name end
    return obj.recordId
end

-- Object under the crosshair and within reach (the engine drops hitObject for
-- out-of-reach hits), i.e. what the activate key would use. Scenery (statics)
-- can't be activated, so it counts as nothing.
local function focusObject()
    local focus = try(camera.getFocusRay)
    local obj = focus and focus.hitObject
    if obj and obj:isValid() and obj.type ~= types.Static then return obj end
    return nil
end

local function stat(fn)
    local s = try(fn)
    if not s then return nil end
    return { current = round(s.current), base = round(s.base) }
end

local function journalTail(n)
    local entries = try(function() return Player.journal(self).journalTextEntries end)
    local out = json.array()
    if not entries then return out end
    local count = #entries
    for i = math.max(1, count - n + 1), count do
        local e = entries[i]
        out[#out + 1] = { quest = e.questId, text = e.text }
    end
    return out
end

local function controlSwitches()
    local cs = Player.CONTROL_SWITCH
    local out = {}
    for _, key in ipairs({ 'Controls', 'Looking', 'Fighting', 'Magic', 'Jumping' }) do
        out[key:lower()] = try(Player.getControlSwitch, self, cs[key])
    end
    return out
end

local function snapshot()
    local cell = self.cell
    local focus = focusObject()
    local modes = json.array()
    modes[1] = I.UI.getMode()
    return {
        cell = cell and {
            name = cell.displayName ~= '' and cell.displayName or cell.name,
            exterior = cell.isExterior,
            grid = cell.isExterior and { cell.gridX, cell.gridY } or nil,
            region = cell.region,
        } or nil,
        pos = { round(self.position.x), round(self.position.y), round(self.position.z) },
        heading = round(math.deg(self.rotation:getYaw()) % 360),
        pitch = round(math.deg(camera.getPitch())),
        ui_modes = modes,
        world_paused = core.isWorldPaused(),
        chargen_done = try(Player.isCharGenFinished, self),
        controls = controlSwitches(),
        stance = try(Actor.getStance, self),
        health = stat(function() return Actor.stats.dynamic.health(self) end),
        magicka = stat(function() return Actor.stats.dynamic.magicka(self) end),
        fatigue = stat(function() return Actor.stats.dynamic.fatigue(self) end),
        crosshair = focus and objectName(focus) or nil,
        journal = journalTail(3),
        game_time = round(core.getGameTime()),
    }
end

-- Action runner ------------------------------------------------------------

local current = nil   -- the command being executed
local lastText = nil  -- last command file contents seen

-- Replies echo seq + nonce so the sidecar never mistakes an old log line
-- (from before it restarted) for the answer to a new command.
local function emit(cmd, result)
    result.seq, result.nonce, result.op = cmd.seq, cmd.nonce, cmd.op
    result.state = result.state or snapshot()
    print('PIAGENT ' .. json.encode(result))
end

local function setPaused(paused)
    core.sendGlobalEvent(paused and 'Pause' or 'Unpause', 'piagent')
end

local function releaseControls()
    self.controls.movement = 0
    self.controls.sideMovement = 0
    self.controls.yawChange = 0
    self.controls.pitchChange = 0
    self.controls.jump = false
    self.controls.use = self.ATTACK_TYPE.NoAttack
    I.Controls.overrideMovementControls(false)
    I.Controls.overrideCombatControls(false)
end

local function finish(outcome, note)
    local cmd = current
    current = nil
    releaseControls()
    setPaused(cmd.op == 'pause' or (cmd.op ~= 'release' and cmd.pause == true))
    emit(cmd, { outcome = outcome, note = note })
end

local function clamp(x, lo, hi)
    if x < lo then return lo elseif x > hi then return hi end
    return x
end

-- Each op: start(cmd) returns an error string to reject the command, or nil;
-- step(cmd, dt) runs on every unpaused frame and returns an outcome to finish.
-- Rejections are only for malformed arguments; an action that has no effect
-- in the game (activating nothing, attacking bare-handed) still finishes as
-- 'done', like a key press that does nothing.
local ops = {}

ops.state = { world = false }
ops.pause = { world = false }

ops.release = {
    world = false,
    start = function() setPaused(false) end,
}

ops.wait = {
    start = function(cmd) cmd.seconds = clamp(tonumber(cmd.seconds) or 1, 0, 30) end,
    step = function(cmd) if cmd.t >= cmd.seconds then return 'done' end end,
    realtime = true,
}

local DIRS = {
    forward = { 1, 0 }, back = { -1, 0 }, backward = { -1, 0 },
    left = { 0, -1 }, right = { 0, 1 },
}

ops.move = {
    start = function(cmd)
        cmd.dirv = DIRS[cmd.dir or 'forward']
        if not cmd.dirv then return 'dir must be forward, back, left or right' end
        cmd.seconds = clamp(tonumber(cmd.seconds) or 1, 0.1, 10)
        cmd.from = self.position
        I.Controls.overrideMovementControls(true)
    end,
    step = function(cmd)
        self.controls.movement = cmd.dirv[1]
        self.controls.sideMovement = cmd.dirv[2]
        self.controls.run = cmd.run ~= false
        if cmd.t >= cmd.seconds then
            local moved = round((self.position - cmd.from):length())
            return 'done', 'moved ' .. moved .. ' units'
        end
    end,
}

-- Relative turn, steered toward a goal fixed at start. A one-shot
-- yawChange/pitchChange can be applied more than once by the engine, so each
-- frame corrects only part of the remaining error, measured on the camera.
local TURN_GAIN = 0.35
local TURN_TOLERANCE = math.rad(0.5)
local MAX_PITCH = math.rad(89)

ops.turn = {
    start = function(cmd)
        local yaw = math.rad(clamp(tonumber(cmd.degrees) or 0, -180, 180))
        local pitch = math.rad(clamp(tonumber(cmd.pitch) or 0, -180, 180))
        cmd.yawGoal = normAngle(camera.getYaw() + yaw)
        cmd.pitchGoal = clamp(camera.getPitch() + pitch, -MAX_PITCH, MAX_PITCH)
    end,
    step = function(cmd)
        local dy = normAngle(cmd.yawGoal - camera.getYaw())
        local dp = cmd.pitchGoal - camera.getPitch()
        local settled = math.abs(dy) < TURN_TOLERANCE and math.abs(dp) < TURN_TOLERANCE
        if settled or cmd.t >= 1.5 then
            self.controls.yawChange, self.controls.pitchChange = 0, 0
            return 'done', string.format('off by yaw %.1f, pitch %.1f deg', math.deg(dy), math.deg(dp))
        end
        self.controls.yawChange = dy * TURN_GAIN
        self.controls.pitchChange = dp * TURN_GAIN
    end,
}

ops.activate = {
    start = function(cmd) cmd.obj = focusObject() end,
    step = function(cmd)
        if not cmd.obj then return 'done', 'nothing in reach under the crosshair' end
        if not cmd.sent then
            core.sendGlobalEvent('PiAgentActivate', { object = cmd.obj, actor = self.object })
            cmd.sent = true
            return
        end
        if cmd.t >= 0.6 then return 'done', 'activated ' .. objectName(cmd.obj) end
    end,
    realtime = true,
}

ops.jump = {
    start = function() I.Controls.overrideMovementControls(true) end,
    step = function(cmd)
        self.controls.jump = not cmd.jumped
        cmd.jumped = true
        if cmd.t >= 0.9 then return 'done' end
    end,
}

local STANCES = { nothing = 'Nothing', weapon = 'Weapon', spell = 'Spell' }

ops.stance = {
    start = function(cmd)
        local key = STANCES[cmd.stance or 'weapon']
        if not key then return 'stance must be nothing, weapon or spell' end
        Actor.setStance(self, Actor.STANCE[key])
    end,
    step = function(cmd) if cmd.t >= 0.8 then return 'done' end end,
}

ops.attack = {
    start = function(cmd)
        cmd.seconds = clamp(tonumber(cmd.seconds) or 0.5, 0.05, 3)
        cmd.unarmed = Actor.getStance(self) == Actor.STANCE.Nothing
        if not cmd.unarmed then I.Controls.overrideCombatControls(true) end
    end,
    step = function(cmd)
        if cmd.unarmed then return 'done', 'no weapon or spell readied' end
        local spell = Actor.getStance(self) == Actor.STANCE.Spell
        if cmd.t < cmd.seconds and not (spell and cmd.cast) then
            self.controls.use = self.ATTACK_TYPE.Any
            cmd.cast = true
        else
            self.controls.use = self.ATTACK_TYPE.NoAttack
        end
        if cmd.t >= cmd.seconds + 0.6 then return 'done' end
    end,
}

local function start(cmd)
    local op = ops[cmd.op]
    if not op then
        emit(cmd, { outcome = 'error', note = 'unknown op' })
        return
    end
    if current then finish('interrupted') end
    cmd.t, cmd.real0 = 0, core.getRealTime()
    current = cmd
    local err = op.start and op.start(cmd)
    if err then
        current = nil
        releaseControls()
        setPaused(cmd.pause == true)
        emit(cmd, { outcome = 'error', note = err })
        return
    end
    if op.world == false then
        finish('done')
    else
        setPaused(false)
    end
end

local lastPoll = 0

local function poll()
    local now = core.getRealTime()
    if now - lastPoll < POLL_INTERVAL then return end
    lastPoll = now
    local f = vfs.open(CMD_FILE)
    if not f then return end
    local text = f:read('*a')
    f:close()
    if text == lastText or text == nil or text:match('^%s*$') then return end
    lastText = text
    local ok, cmd = pcall(function() return util.loadCode('return ' .. text, {})() end)
    if not ok or type(cmd) ~= 'table' or not cmd.op then
        emit({}, { outcome = 'error', note = 'bad command: ' .. tostring(cmd) })
        return
    end
    if cmd.op ~= 'noop' then start(cmd) end
end

local function onFrame(dt)
    poll()
    local cmd = current
    if not cmd then return end
    local op = ops[cmd.op]
    local realElapsed = core.getRealTime() - cmd.real0
    if op.realtime then
        cmd.t = realElapsed
    elseif core.isWorldPaused() then
        -- A menu (dialogue, message box) holds the world; don't wait forever.
        local mode = I.UI.getMode()
        if realElapsed > 1.0 and mode then
            finish('blocked', 'a menu is open: ' .. mode)
        end
        return
    else
        cmd.t = cmd.t + dt
    end
    local outcome, note = op.step(cmd, dt)
    if outcome then
        finish(outcome, note)
    elseif realElapsed > 45 then
        finish('timeout', 'action ran too long')
    end
end

return {
    engineHandlers = {
        onFrame = onFrame,
    },
}
