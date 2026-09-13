#include <iostream>
#include <string_view>

#include "version.hpp"

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--version") {
        std::cout << afrunner::runner_version << '\n';
        return 0;
    }

    std::cerr << "afrunner: usage error\n";
    return 2;
}
