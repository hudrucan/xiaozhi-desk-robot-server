import argparse
import asyncio
import importlib.util
import os


def list_performance_tester_modules():
    performance_tester_dir = os.path.join(
        os.path.dirname(__file__), "performance_tester"
    )
    return sorted(
        file[:-3]
        for file in os.listdir(performance_tester_dir)
        if file.startswith("performance_tester_") and file.endswith(".py")
    )


def load_module(module_name):
    module_path = os.path.join(
        os.path.dirname(__file__), "performance_tester", f"{module_name}.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load performance tester: {module_name}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def execute_module(module, module_name):
    main_func = getattr(module, "main", None)
    if main_func is None:
        raise RuntimeError(f"No main function found in {module_name}")

    if asyncio.iscoroutinefunction(main_func):
        await main_func()
    else:
        main_func()


def get_module_description(module_name):
    module = load_module(module_name)
    return getattr(module, "description", "No description available")


def parse_args():
    parser = argparse.ArgumentParser(description="Run a provider performance test")
    parser.add_argument(
        "module",
        nargs="?",
        help="Tester module, for example performance_tester_tts",
    )
    return parser.parse_args()


def select_module(modules, requested_module):
    if requested_module:
        module_name = requested_module.removesuffix(".py")
        if module_name not in modules:
            available = ", ".join(modules)
            raise ValueError(
                f"Unknown performance tester: {module_name}. Available: {available}"
            )
        return module_name

    print("Available performance testers:")
    for index, module_name in enumerate(modules, 1):
        description = get_module_description(module_name)
        print(f"{index}. {module_name} - {description}")

    choice = int(input("Select a performance tester: ")) - 1
    if choice < 0 or choice >= len(modules):
        raise ValueError("Invalid selection")
    return modules[choice]


def main():
    modules = list_performance_tester_modules()
    if not modules:
        print("No performance testers are available.")
        return

    args = parse_args()
    try:
        module_name = select_module(modules, args.module)
        module = load_module(module_name)
        asyncio.run(execute_module(module, module_name))
    except (ValueError, RuntimeError) as error:
        print(error)


if __name__ == "__main__":
    main()
