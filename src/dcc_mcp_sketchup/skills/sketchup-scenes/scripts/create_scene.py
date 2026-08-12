from dcc_mcp_core.skill import run_main

from dcc_mcp_sketchup.skill_tools import bridge_main

main = bridge_main("scenes.create", "SketchUp scene created.")

if __name__ == "__main__":
    run_main(main)
