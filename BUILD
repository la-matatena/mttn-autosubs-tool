load("//third_party/bazel_rules/rules_python/python:py_binary.bzl", "py_binary")
load("//third_party/bazel_rules/rules_python/python:py_library.bzl", "py_library")
load("//third_party/bazel_rules/rules_python/python:py_test.bzl", "py_test")

py_library(
    name = "autosubs_es_lib",
    srcs = [
        "autosubs_server.py",
        "exporters.py",
        "font_manager.py",
        "hardware_imac2017.py",
        "package_bundle.py",
        "resolve_bridge.py",
        "spanish_nlp.py",
    ],
    data = [
        ".gitignore",
        "BUILD",
        "Iniciar AutoSubs ES.command",
        "README.md",
        "autosubs_es_test.py",
        "install_imac.sh",
        "pyproject.toml",
    ] + glob([
        "docs/*.md",
        "docs/images/*.png",
        "lua/*.lua",
        "templates/*.setting",
        "ui/*",
    ]),
)

py_binary(
    name = "autosubs_es_server",
    srcs = ["autosubs_server.py"],
    main = "autosubs_server.py",
    deps = [":autosubs_es_lib"],
)

py_binary(
    name = "package_bundle",
    srcs = ["package_bundle.py"],
    main = "package_bundle.py",
    deps = [":autosubs_es_lib"],
)

py_test(
    name = "autosubs_es_test",
    srcs = ["autosubs_es_test.py"],
    main = "autosubs_es_test.py",
    deps = [":autosubs_es_lib"],
)
