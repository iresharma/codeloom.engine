HANDLERS: dict[type, object] = {}


def handles(command_type: type):
    def deco(fn):
        HANDLERS[command_type] = fn
        return fn

    return deco
