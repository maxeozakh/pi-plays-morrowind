-- Global half of the agent mod: things a player script may not do itself.
return {
    eventHandlers = {
        PiAgentActivate = function(data)
            if data.object and data.object:isValid() then
                data.object:activateBy(data.actor)
            end
        end,
    },
}
