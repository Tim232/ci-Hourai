using System.Threading.Tasks;
using Discord;
using Discord.Commands;
using Hourai.Model;
using Hourai.Preconditions;

namespace Hourai.Standard {

public partial class Standard {

  [Group("command")]
  [RequireContext(ContextType.Guild)]
  public class Commands : DatabaseHouraiModule {

    public Commands(DatabaseService db) : base(db) {
    }

    [Log]
    [Command]
    [GuildRateLimit(1, 1)]
    [MinimumRole(MinimumRole.Command)]
    [Remarks("Creates a custom command. Deletes an existing one if response is empty.")]
    public async Task CreateCommand(string name,
                                    [Remainder] string response = "") {
      var command = Context.DbGuild.GetCustomCommand(name);
      if (string.IsNullOrEmpty(response)) {
        if (command == null) {
          await RespondAsync($"Command {name.Code()} does not exist and thus cannot be deleted.");
          return;
        }
        Context.DbGuild.Commands.Remove(command);
        DbContext.Commands.Remove(command);
        await DbContext.Save();
        await RespondAsync($"Custom command {name.Code()} has been deleted.");
        return;
      }
      string action;
      if (command == null) {
        command = new CustomCommand {
          Name = name,
          Response = response,
          Guild = Context.DbGuild
        };
        Context.DbGuild.Commands.Add(command);
        action = "created";
      } else {
        command.Response = response;
        action = "updated";
      }
      await DbContext.Save();
      await Success($"Command {name.Code()} {action} with response {response}.");
    }

    [Command("dump")]
    [Remarks("Dumps the base source text for a command.")]
    public Task CommandDump(string command) {
      var customCommand = Context.DbGuild.GetCustomCommand(command);
      if(customCommand == null)
        return RespondAsync($"No custom command named {command}");
      else
        return RespondAsync($"{customCommand.Name}: {customCommand.Response}");
    }

    [Log]
    [Command("role")]
    [ServerOwner]
    [Remarks("Sets the minimum role for creating custom commands.")]
    public async Task CommandRole(IRole role) {
      Context.DbGuild.SetMinimumRole(MinimumRole.Command, role);
      await Success($"Set {role.Name.Code()} as the minimum role to create custom commnds.");
    }

  }

}

}
