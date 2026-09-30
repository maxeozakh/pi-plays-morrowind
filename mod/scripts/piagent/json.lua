-- Minimal JSON encoder: OpenMW's Lua sandbox ships none. Tables marked with
-- json.array() always encode as arrays, so an empty list stays [].
local json = {}

local ARRAY = {}

function json.array(t)
    return setmetatable(t or {}, ARRAY)
end

local escapes = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function encodeString(s)
    return '"' .. (s:gsub('[%c"\\]', function(c)
        return escapes[c] or string.format('\\u%04x', c:byte())
    end)) .. '"'
end

local encode

local function isArray(t)
    if getmetatable(t) == ARRAY then return true end
    local n = #t
    if n == 0 then return false end
    for k in pairs(t) do
        if type(k) ~= 'number' or k < 1 or k > n or k % 1 ~= 0 then return false end
    end
    return true
end

encode = function(v)
    local t = type(v)
    if t == 'nil' then
        return 'null'
    elseif t == 'boolean' then
        return tostring(v)
    elseif t == 'number' then
        if v ~= v or v == math.huge or v == -math.huge then return 'null' end
        if v % 1 == 0 and math.abs(v) < 1e15 then return string.format('%d', v) end
        return string.format('%.3f', v)
    elseif t == 'string' then
        return encodeString(v)
    elseif t == 'table' then
        local parts = {}
        if isArray(v) then
            for i = 1, #v do parts[i] = encode(v[i]) end
            return '[' .. table.concat(parts, ',') .. ']'
        end
        for k, val in pairs(v) do
            parts[#parts + 1] = encodeString(tostring(k)) .. ':' .. encode(val)
        end
        return '{' .. table.concat(parts, ',') .. '}'
    end
    return encodeString(tostring(v))
end

json.encode = encode

return json
