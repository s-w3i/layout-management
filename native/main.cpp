#include "model.hpp"
#include <fstream>
#include <iostream>

int main(int argc, char **argv) {
  using namespace layout;
  try {
    json request;
    if (argc > 1) {
      std::ifstream stream(argv[1]);
      if (!stream)
        throw std::runtime_error("Cannot open request file");
      stream >> request;
    } else
      std::cin >> request;
    if (!request.is_object())
      throw std::runtime_error("Request must be an object");
    auto command = text(request, "command", "generate");
    json result;
    if (command == "geometry")
      result = geometry_result(geometry(request.at("project")));
    else if (command == "analyze")
      result = analyze(request);
    else if (command == "stock")
      result = calculate_stock(request);
    else if (command == "generate")
      result = generate(request);
    else if (command == "validate")
      result = validate_layout(request);
    else if (command == "score")
      result = score_layout(request);
    else if (command == "movement")
      result = movement_metrics(request);
    else if (command == "swap")
      result = swap_layout(request);
    else
      throw std::runtime_error("Unknown engine command: " + command);
    std::cout << result.dump() << '\n';
    return 0;
  } catch (const std::exception &e) {
    std::cout << json({{"error", e.what()}}).dump() << '\n';
    return 1;
  }
}
