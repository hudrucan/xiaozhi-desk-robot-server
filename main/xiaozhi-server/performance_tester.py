import argparse
import asyncio
import importlib.util
import os


def list_performance_tester_modules():
    performance_tester_dir = os.path.join(
        os.path.dirname(__file__), "performance_testers"
    )
    return sorted(
        file.removeprefix("performance_tester_").removesuffix(".py")
        for file in os.listdir(performance_tester_dir)
        if file.startswith("performance_tester_") and file.endswith(".py")
    )


def list_performance_tester_groups():
    performance_tester_dir = os.path.join(
        os.path.dirname(__file__), "performance_testers"
    )
    groups = {}
    for entry in sorted(os.listdir(performance_tester_dir)):
        group_dir = os.path.join(performance_tester_dir, entry)
        if entry.startswith("_") or not os.path.isdir(group_dir):
            continue
        modules = sorted(
            file.removeprefix("performance_tester_").removesuffix(".py")
            for file in os.listdir(group_dir)
            if file.startswith("performance_tester_") and file.endswith(".py")
        )
        if modules:
            groups[entry] = modules
    return groups


def load_module(module_name, group=None):
    path_parts = [os.path.dirname(__file__), "performance_testers"]
    if group:
        path_parts.append(group)
    module_path = os.path.join(
        *path_parts,
        f"performance_tester_{module_name}.py",
    )
    import_name = f"{group}_{module_name}" if group else module_name
    spec = importlib.util.spec_from_file_location(import_name, module_path)
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


def get_module_description(module_name, group=None):
    module = load_module(module_name, group)
    return getattr(module, "description", "No description available")


def parse_args(modules, groups):
    parser = argparse.ArgumentParser(description="Run a provider performance test")
    parser.add_argument(
        "module",
        nargs="?",
        choices=modules + list(groups),
        help="Provider benchmark or benchmark group to run",
    )
    parser.add_argument(
        "group_module",
        nargs="?",
        help="Benchmark inside the selected group",
    )
    return parser.parse_args()


def select_module(modules, groups, requested_module, requested_group_module):
    if requested_module:
        if requested_module in groups:
            if not requested_group_module:
                available = ", ".join(groups[requested_module])
                raise ValueError(
                    f"Select a {requested_module} tester: {available}"
                )
            if requested_group_module not in groups[requested_module]:
                available = ", ".join(groups[requested_module])
                raise ValueError(
                    f"Unknown {requested_module} tester: "
                    f"{requested_group_module}. Available: {available}"
                )
            return requested_module, requested_group_module
        if requested_group_module:
            raise ValueError(
                f"{requested_module} does not accept a nested tester"
            )
        return None, requested_module

    print("Available performance testers:")
    targets = [(None, module_name) for module_name in modules]
    targets.extend(
        (group, module_name)
        for group, group_modules in groups.items()
        for module_name in group_modules
    )
    for index, (group, module_name) in enumerate(targets, 1):
        description = get_module_description(module_name, group)
        label = f"{group} {module_name}" if group else module_name
        print(f"{index}. {label} - {description}")

    choice = int(input("Select a performance tester: ")) - 1
    if choice < 0 or choice >= len(targets):
        raise ValueError("Invalid selection")
    return targets[choice]


def main():
    modules = list_performance_tester_modules()
    groups = list_performance_tester_groups()
    if not modules and not groups:
        print("No performance testers are available.")
        return

    args = parse_args(modules, groups)
    try:
        group, module_name = select_module(
            modules,
            groups,
            args.module,
            args.group_module,
        )
        module = load_module(module_name, group)
        display_name = f"{group} {module_name}" if group else module_name
        asyncio.run(execute_module(module, display_name))
    except (ValueError, RuntimeError) as error:
        print(error)


if __name__ == "__main__":
    main()
