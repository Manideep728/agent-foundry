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

add_executable(afrunner src/main.cpp)
target_link_libraries(afrunner PRIVATE afrunner_flags)

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

    add_executable(runner_tests tests/test_version.cpp)
    target_link_libraries(runner_tests PRIVATE afrunner_flags GTest::gtest_main)

    # Container-only cases carry the "container" label instead, so
    # `ctest -L native` is the suite that runs without Docker.
    gtest_discover_tests(runner_tests PROPERTIES LABELS native)
endif()
