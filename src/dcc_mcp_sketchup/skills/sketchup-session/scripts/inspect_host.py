from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_sketchup.bridge import get_bridge


@skill_entry
def main(**_kwargs):
    result = get_bridge().call("sketchup.inspect_model")
    return skill_success("SketchUp model inspected.", model=result)


if __name__ == "__main__":
    run_main(main)
