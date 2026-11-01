"""Copy authoritative runtime schemas into wheels without a source mirror."""
from pathlib import Path
from shutil import copyfile
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSchemas(build_py):
    def run(self):
        super().run()
        destination = Path(self.build_lib) / "agent_harness" / "schemas"
        destination.mkdir(parents=True, exist_ok=True)
        for source in sorted((Path(__file__).parent / "schemas").glob("*.json")):
            copyfile(source, destination / source.name)


setup(cmdclass={"build_py": BuildWithSchemas})
