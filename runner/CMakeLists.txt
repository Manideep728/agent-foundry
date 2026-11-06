cmake_minimum_required(VERSION 3.20)

project(afrunner LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(AFRUNNER_BUILD_TESTS "Build the runner test suite" ON)

# Carried by our own targets only. -Werror applied globally would also reach
# fetched dependencies, and a warning in someone else's pinned source would
# fail our build.
add_library(afrunner_flags INTERFACE)
target_include_directories(afrunner_flags INTERFACE src)
target_compile_options(afrunner_flags INTERFACE -Wall -Wextra -Werror)

# Everything except main.cpp, so the test binary can link the same objects
# without inheriting a second main().
add_library(afrunner_core OBJECT
    src/cgroup.cpp
    src/child.cpp
    src/clock.cpp
    src/commands.cpp
    src/json.cpp
    src/options.cpp
    src/outcome.cpp
    src/report.cpp
    src/resultfile.cpp
    src/signals.cpp
    src/sink.cpp
    src/supervisor.cpp
    src/sysinfo.cpp
    src/tail.cpp
)
target_link_libraries(afrunner_core PUBLIC afrunner_flags)

add_executable(afrunner src/main.cpp)
target_link_libraries(afrunner PRIVATE afrunner_core)

enable_testing()

add_test(NAME version_flag COMMAND afrunner --version)
set_tests_properties(version_flag PROPERTIES
    PASS_REGULAR_EXPRESSION "afrunner [0-9]+\\.[0-9]+\\.[0-9]+"
    LABELS native
)

if(AFRUNNER_BUILD_TESTS)
    include(FetchContent)
    FetchContent_Declare(
        googletest
        GIT_REPOSITORY https://github.com/google/googletest.git
        GIT_TAG v1.15.2
        GIT_SHALLOW TRUE
    )
    set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(googletest)

    include(GoogleTest)

    add_executable(runner_tests
        tests/test_version.cpp
        tests/test_cgroup.cpp
        tests/test_child.cpp
        tests/test_commands.cpp
        tests/test_json.cpp
        tests/test_options.cpp
        tests/test_outcome.cpp
        tests/test_report.cpp
        tests/test_signals.cpp
        tests/test_supervisor.cpp
        tests/test_resultfile.cpp
        tests/test_sysinfo.cpp
        tests/test_tail.cpp
    )
    target_link_libraries(runner_tests PRIVATE afrunner_core GTest::gtest_main)

    # Every gtest case runs without Docker. The behaviour that needs a real
    # container, PID 1 above all, is tested from outside it in
    # tests/test_runner_container.py.
    gtest_discover_tests(runner_tests PROPERTIES LABELS native TIMEOUT 60)
endif()
