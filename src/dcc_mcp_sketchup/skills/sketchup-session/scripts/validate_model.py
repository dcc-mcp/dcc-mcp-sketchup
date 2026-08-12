from dcc_mcp_core.skill import run_main

from dcc_mcp_sketchup.skill_tools import bridge_main

main = bridge_main("model.validate", "SketchUp model validated.")

if __name__ == "__main__":
    run_main(main)
