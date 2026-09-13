cmake_minimum_required(VERSION 3.20)

project(afrunner LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(afrunner src/main.cpp)
target_include_directories(afrunner PRIVATE src)

# Scoped to our own target rather than set globally: -Werror applied through
# add_compile_options would also reach fetched dependencies, and a warning in
# someone else's pinned source would fail our build.
target_compile_options(afrunner PRIVATE -Wall -Wextra -Werror)

enable_testing()

add_test(NAME version_flag COMMAND afrunner --version)
set_tests_properties(version_flag PROPERTIES
    PASS_REGULAR_EXPRESSION "afrunner [0-9]+\\.[0-9]+\\.[0-9]+"
    LABELS native
)
