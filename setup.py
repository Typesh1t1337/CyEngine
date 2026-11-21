from setuptools import setup, Extension, find_packages
from Cython.Build import cythonize

extensions = [
    Extension(
        "cyengine.core._types",
        sources=["cyengine/core/_types.pyx"],
        extra_compile_args=["-O3"],
    ),
    Extension(
        "cyengine.core._router",
        sources=["cyengine/core/_router.pyx"],
        extra_compile_args=["-O3"],
    ),
    Extension(
        "cyengine.core._protocol",
        sources=["cyengine/core/_protocol.pyx"],
        extra_compile_args=["-O3"],
    ),
    Extension(
        "cyengine.core._middleware",
        sources=["cyengine/core/_middleware.pyx"],
        extra_compile_args=["-O3"],
    ),
]

setup(
    name="cyengine",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=["httptools", "orjson"],
    extras_require={"uvloop": ["uvloop"], "pydantic": ["pydantic>=2"]},
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": 3,
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "nonecheck": False,
        },
    ),
)