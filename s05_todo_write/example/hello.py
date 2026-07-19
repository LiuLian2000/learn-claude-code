def greet(name: str) -> None:
    """Greet a person by printing a hello message.

    Args:
        name: The name of the person to greet.
    """
    message = "Hello, " + name
    print(message)


def main() -> None:
    """Entry point for the script."""
    greet("Claude")


if __name__ == "__main__":
    main()
