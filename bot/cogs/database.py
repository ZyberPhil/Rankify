from __future__ import annotations

from discord.ext import commands


class DatabaseCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if getattr(self.bot, "_db_ready_logged", False):
            return
        self.bot._db_ready_logged = True
        self.bot.logger.info("DatabaseCog is ready.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DatabaseCog(bot))
