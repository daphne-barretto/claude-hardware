"""What no check tests (`geometry.report` lists them as untested). Kept apart from the report,
which loads the CAD kernel, so the server can read it (`app.agent.safety`)."""

UNTESTED = {
    "strength": ("Strength", "Loads on the parts, snap fits and screw bosses are not simulated."),
    "drop": ("Drop", "Impact from a drop is not simulated."),
    "heat": ("Heat", "Temperature rise and heat from the electronics are not simulated."),
}
